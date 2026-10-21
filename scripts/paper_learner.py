#!/usr/bin/env python3
"""Paper-trading accuracy improvement loop — runs constantly, learns, adapts.

Strategy: simulate the validated capitulation-catch logic on HISTORICAL data
(never touching live money) across many symbols/timeframes, record every
paper trade outcome, and re-fit the strategy's dip/RSI/hold parameters to
maximize NET expectancy (after fees). A new param set is adopted only if it
beats the current one by >= 0.05pp of net expectancy.

HONEST SCOPE (corrected 2026-10-07):
  * Ranking is by NET EV, never accuracy — "accuracy" was the project's
    documented failure mode (SYSTEM_MAP §5), and GROSS expectancy ignores the
    fee wall (fees.py) that is 100% of the loss. See the fee-honesty note at
    the FEE_RT import below.
  * There is NO out-of-sample split here. The sweep is historical and live is
    unseen, which is an IMPLICIT guard only; it is not a substitute for the
    walk-forward engines (replay_engine / hunt.py). Do not read this learner's
    number as a validated edge.
  * The adopted params are a SIGNAL, not a control: no live/paper trader reads
    paper-learner-state.json (unified_trader holds its own buckets). The log
    says so on every adoption.

Pure stdlib (no numpy/sklearn) so it runs on the tiny VPS.

Run: cron daily (see crontab), or `python paper_learner.py [once|retrain]`
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SWEEP = DATA / "full_sweep.json"
DB = DATA / "trading.db"
STATE = DATA / "paper-learner-state.json"

# ── fee honesty (2026-10-07) ────────────────────────────────────────────────
# This learner used to score every parameter set on GROSS expectancy and rank
# by ACCURACY. Both are the failure modes this project already documented:
#   * fees.py: "Our gross edge is ~0; fees were 100% of the loss."
#   * SYSTEM_MAP §5: "Dip-buying / accuracy: 66% win rate still LOSES."
# Measured against the state file on 2026-10-07: all 72 configs were
# GROSS-positive, but only 19 survive the measured round-trip cost — and every
# dip shallower than -4% is NET-NEGATIVE (0 of 48 net-positive). A gate of
# `ev > 0` (gross) therefore admits guaranteed net losers. It only happened to
# select a deep dip by accident of the accuracy gate.
#
# FEE_RT is the measured round-trip cost from fees.py (0.1594%, n=82 fills).
# Imported, never re-typed, so it cannot silently drift from the one reader.
try:
    sys.path.insert(0, str(ROOT / "scripts"))
    from fees import ROUND_TRIP as FEE_RT  # round-trip cost as a fraction
except Exception:  # noqa: BLE001 — data/analysis path must not crash on import
    FEE_RT = 0.001594

# Symbols/timeframes we have data for in full_sweep.json
SERIES_KEYS = [
    "5m:BTC/USDT", "15m:BTC/USDT", "30m:BTC/USDT",
    "15m:ETH/USDT", "30m:ETH/USDT",
    "30m:XRP/USDT",
    "15m:SOL/USDT", "30m:SOL/USDT",
]

# Parameter search space (kept small for speed on VPS)
DIPS = [-0.005, -0.01, -0.02, -0.04, -0.06, -0.08]
RSIS = [30, 35, 40, 45]
REDS = [2, 3, 4]
HOLDS = {"5m": 6, "15m": 4, "30m": 3}  # bars held per tf


_LOAD_STAMP = {}  # key -> (source label, mtime) for the series actually scored
_LOAD_WINDOW = {}  # key -> (first_open_ms, last_open_ms) actually scored


def _db_series(db_path):
    """Load every SERIES_KEY straight from the live candle store.

    Returns ({key: [(open_time_ms, close), ...]}, {key: mtime}, {key: (first, last)}).

    This is the primary source (2026-10-20). The learner used to score
    data/live_fine.json + data/alts_fine.json, both frozen at 2026-08-15, so
    its verdict described a market seven weeks gone while the DB carried
    candles to today. Four keys (BTC 5m/15m/30m, XRP 30m) exist ONLY in those
    sweep files, so the files stay as a labelled fallback for them.

    Never raises: a missing/unreadable DB returns ({}, {}, {}) and the caller
    falls back, printing which source it used.
    """
    out, mt, win = {}, {}, {}
    try:
        if not Path(db_path).exists():
            return out, mt, win
        import sqlite3
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for key in SERIES_KEYS:
                tf, sym = key.split(":", 1)
                rows = con.execute(
                    "SELECT open_time_utc, close FROM candles "
                    "WHERE symbol=? AND timeframe=? ORDER BY open_time_utc",
                    (sym, tf),
                ).fetchall()
                if rows:
                    out[key] = [(r[0], r[1]) for r in rows]
                    win[key] = (rows[0][0], rows[-1][0])
                    mt[key] = Path(db_path).stat().st_mtime
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — data layer is best-effort
        return {}, {}, {}
    return out, mt, win


def _read_sweep_file(path):
    """Read one historical-sweep JSON, returning {key: [(ts, close), ...]}.

    Returns {} on any failure so a missing/corrupt file never raises."""
    try:
        if not path.exists():
            return {}
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — data files are best-effort inputs
        return {}


def _load_all_series():
    """Load the close series ONCE into {key: closes}, DB first, sweeps as fallback.

    Bug fixed 2026-10-05: this used to read data/full_sweep.json, which
    disk_cleanup.sh deleted as 'referenced NOWHERE' — but this cron DOES
    reference it, so paper_learner crashed with FileNotFoundError on every
    daily run and silently never learned. Fall back to the sibling sweep
    files that still exist (live_fine.json / alts_fine.json) and map their
    keys onto SERIES_KEYS. If no source has usable data, return {} so main()
    logs a clear line and exits 0 instead of dumping a traceback.

    Bug fixed 2026-10-20: the fallbacks were the ONLY sources, and both are
    frozen at 2026-08-15. The DB is now the primary source; the sweep files
    are consulted only for keys the DB cannot serve, and every series is
    stamped with its source AND its real first/last bar so the verdict can
    state the window it was measured on.
    """
    out, stamp, window = {}, {}, {}

    db_series, db_mt, db_win = _db_series(DB)
    for key, series in db_series.items():
        out[key] = [x[1] for x in series]
        stamp[key] = ("trading.db", db_mt.get(key, 0.0))
        window[key] = db_win[key]

    missing = [k for k in SERIES_KEYS if k not in out]
    if missing:
        sources = [SWEEP, DATA / "live_fine.json", DATA / "alts_fine.json"]
        merged = {}
        origin = {}   # raw key -> (source basename, mtime)
        for path in sources:
            for k, v in _read_sweep_file(path).items():
                if k not in merged and isinstance(v, list) and v:
                    merged[k] = v
                    try:
                        origin[k] = (path.name, path.stat().st_mtime)
                    except OSError:
                        origin[k] = (path.name, 0.0)
        # TUPLE-SHAPE GUARD (2026-10-20): the files above are CONSOLIDATED
        # sweep dumps ({key: [(ts, close), ...]}), but the bug note below also
        # cites caches like /tmp/cache-head.json that store BARE closes
        # ([float, ...]). x[1] on a float raises "TypeError: 'float' is not
        # subscriptable" — which is NOT caught here (it happens before the try
        # below), so one malformed source killed the whole grid. Accept both
        # shapes explicitly and let anything else fall through.
        for k, v in merged.items():
            if v and not isinstance(v[0], (list, tuple)):
                merged[k] = [(i, c) for i, c in enumerate(v)]
        # live_fine.json is keyed by bare timeframe ("15m") for BTC/USDT only.
        for key in missing:
            series = merged.get(key)
            src_key = key
            if series is None and key in ("15m:BTC/USDT", "30m:BTC/USDT",
                                          "5m:BTC/USDT"):
                bare = key.split(":", 1)[0]
                series = merged.get(bare)
                src_key = bare
            if not series:
                continue
            try:
                ts_list = [x[0] for x in series]
                out[key] = [x[1] for x in series]
            except Exception:  # noqa: BLE001
                continue
            if src_key in origin:
                stamp[key] = origin[src_key]
            try:
                window[key] = (min(ts_list), max(ts_list))
            except Exception:  # noqa: BLE001
                pass

    # Only stamp/serve keys actually scored, so a caller iterating `out` can
    # always answer "which source, which window" for the data it just used.
    _LOAD_STAMP.clear()
    _LOAD_WINDOW.clear()
    for key in out:
        _LOAD_STAMP[key] = stamp[key]
        if key in window:
            _LOAD_WINDOW[key] = window[key]
    return out


def evaluate_params(dip, rsi_max, red_min, series_cache):
    """Average accuracy + EV across all series for a param set."""
    accs, evs, ns = [], [], []
    for key in SERIES_KEYS:
        closes = series_cache.get(key)
        if not closes or len(closes) < 120:
            continue
        tf = key.split(":")[0]
        r = simulate(closes, dip, rsi_max, red_min, HOLDS[tf])
        if r:
            w, l, ev = r
            accs.append(w / (w + l)); evs.append(ev); ns.append(w + l)
    if not accs:
        return None
    tot = sum(ns)
    wacc = sum(a * n for a, n in zip(accs, ns)) / tot
    wev = sum(e * n for e, n in zip(evs, ns)) / tot
    return wacc * 100, wev, tot


def _rsi(closes, i, n=14):
    if i < n:
        return 50.0
    g = l = 0.0
    for j in range(i - n + 1, i + 1):
        dx = closes[j] - closes[j - 1]
        if dx > 0: g += dx
        else: l -= dx
    if l == 0: return 100.0
    rs = (g / n) / (l / n)
    return 100.0 - 100.0 / (1 + rs)


def _rolling_max(closes, i, w=90):
    return max(closes[max(0, i - w):i]) if i > 0 else closes[0]


def simulate(closes, dip, rsi_max, red_min, hold_bars, fee_rt=FEE_RT):
    """Backtest the capitulation rule on a close series. Returns (wins, losses, ev%).

    `ev` is NET of the measured round-trip fee (fees.py). Returns are reduced by
    `fee_rt` before being counted, so a trade whose move is smaller than the
    cost counts as the loss it is in reality. This is the fix for the defect
    where this learner, alone among the system's loops, scored on GROSS.
    """
    wins = losses = 0
    wret, lret = [], []
    n = len(closes)
    for i in range(90, n - hold_bars):
        hi = _rolling_max(closes, i)
        dd = closes[i] / hi - 1 if hi > 0 else 0
        if dd > dip:
            continue
        rsi = _rsi(closes, i)
        if rsi > rsi_max:
            continue
        red = 0
        j = i
        while j > 0 and closes[j] < closes[j - 1]:
            red += 1; j -= 1
        if red < red_min:
            continue
        entry = closes[i]
        exit_p = closes[i + hold_bars]
        ret = exit_p / entry - 1 - fee_rt        # NET of round-trip cost
        if ret > 0:
            wins += 1; wret.append(ret)
        else:
            losses += 1; lret.append(ret)
    total = wins + losses
    if total < 10:
        return None
    ev = (sum(wret) + sum(lret)) / total * 100 if total else 0
    return wins, losses, ev


def _input_signature(series_cache):
    """Digest of WHAT was scored: each series' length + last close + last ts.

    Deliberately cheap — it runs before the grid, and its only job is to make a
    cached grid from a different input (frozen file vs live DB, or yesterday's
    bars) impossible to reuse silently. Returns "" when there is nothing to
    digest, which never matches a stored stamp (so the cache is rebuilt).
    """
    import hashlib
    h = hashlib.sha256()
    for key in sorted(series_cache):
        closes = series_cache[key]
        if not closes:
            continue
        last_ts = _LOAD_WINDOW.get(key, (0, 0))[1]
        h.update(f"{key}:{len(closes)}:{closes[-1]!r}:{last_ts};".encode())
    return h.hexdigest()[:16] if series_cache else ""


def best_params():
    """Grid-search the param space for highest NET expectancy. Pure python,
    cached per-combo so it survives interruption (writes progress to STATE).

    SELECTION FIX (2026-10-07): this used to rank by ACCURACY once a config
    cleared `ev > 0`. `ev` is now net of fees (see simulate), and the ranking
    is by NET EV — the quantity that compounds — with accuracy only a
    tiebreak. Ranking by accuracy is the documented failure mode
    (SYSTEM_MAP §5: "66% win rate still LOSES"); it also picked a
    higher-accuracy config over a genuinely better net-EV one on the current
    state. The gate keeps an accuracy floor of 55% so we do not adopt a config
    that wins by rare huge outliers while losing the median trade.
    """
    combos = [(d, r, rm) for d in DIPS for r in RSIS for rm in REDS]
    series_cache = _load_all_series()  # load sweep data ONCE
    if not series_cache:
        print("paper-learner: no historical sweep data available "
              "(full_sweep.json missing and no fallback series) — skipping")
        return None
    # load prior progress.
    # 🔴 CACHE-UNITS GUARD (2026-10-07): the cached `scored` values are only
    # comparable if they were computed with the SAME fee applied. Before this
    # date they were GROSS; now they are NET. Mixing the two in one grid would
    # gate a gross figure with a net threshold and silently mis-rank configs —
    # the same class of silent-unit error book_units.py exists to stop. So the
    # cache is stamped with the round-trip cost it was built under and is
    # discarded wholesale when that stamp does not match.
    done = {}
    if STATE.exists():
        try:
            blob = json.loads(STATE.read_text())
            # SOURCE-STAMP GUARD (2026-10-20): a cached `scored` value is only
            # comparable to a fresh one if it was computed on the SAME data.
            # Before the DB became the primary source the grid was built from
            # files frozen at 2026-08-15; keeping those numbers after the fix
            # would have made the learner print the old verdict forever and
            # never re-measure — the exact "stale INPUT voids the inference"
            # failure this file's INPUT line was added to expose. The stamp is
            # a digest of each scored series' length + last bar, so ANY source
            # change (or new bars) invalidates the cache.
            if (abs(blob.get("cost_rt", -1.0) - FEE_RT) < 1e-12
                    and blob.get("input_sig")
                    and blob.get("input_sig") == _input_signature(series_cache)):
                done = {tuple(eval(k)): v for k, v in blob.get("scored", {}).items()}
            elif blob.get("scored"):
                print("paper-learner: cached grid was scored on DIFFERENT data "
                      f"(cost_rt={blob.get('cost_rt')}, input_sig "
                      f"{str(blob.get('input_sig'))[:12] or 'absent'} != "
                      f"{_input_signature(series_cache)[:12]}) — recomputing")
        except Exception:
            done = {}
    best = None
    for dip, rsi_max, red_min in combos:
        key = (dip, rsi_max, red_min)
        if key in done:
            r = done[key]
        else:
            r = evaluate_params(dip, rsi_max, red_min, series_cache)
            if r:
                done[key] = r
                try:
                    s = {"cost_rt": FEE_RT,
                         "input_sig": _input_signature(series_cache),
                         "scored": {str(list(k)): v for k, v in done.items()}}
                    STATE.write_text(json.dumps(s), encoding="utf-8")
                except Exception:
                    pass
        if not r:
            continue
        acc, ev, n = r
        # ev is NET (fees charged in simulate). Gate: net-positive, an accuracy
        # floor, and enough trades. Rank by net EV, accuracy as tiebreak.
        if ev > 0 and acc >= 55 and n >= 200:
            score = (ev, acc)
            # best = (ev, dip, rsi_max, red_min, ev, n, acc); the tiebreak is
            # accuracy, i.e. best[6] — NOT best[1] (which is dip).
            if best is None or score > (best[0], best[6]):
                best = (ev, dip, rsi_max, red_min, ev, n, acc)
    return best


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    # Load or init state
    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except Exception:
            pass
    current = state.get("current_params", {"dip": -0.02, "rsi_max": 30, "red_min": 3,
                                           "acc": 0, "ev_net": 0})
    # 🔴 LEGACY-UNIT GUARD (2026-10-07): the pre-fix state stored a GROSS figure
    # under key "ev". Reading that as ev_net would compare a gross number to a
    # net one and make the net config look like a downgrade — which is exactly
    # what happened on the first run after the fix (gross 0.326 vs net 0.184,
    # so it "kept current" for the wrong reason). Only trust "ev_net"; if it is
    # absent the current config has no comparable net value, so seed it from
    # this pass's own grid for the SAME config rather than from the stale gross.
    if "ev_net" not in current and "ev" in current:
        current["ev_net"] = None   # unknown, forces a clean re-adopt below
    print(f"paper-learner: searching param space ({len(DIPS)*len(RSIS)*len(REDS)} combos)...")
    best = best_params()
    # best_params() persisted the freshly-scored grid ({cost_rt, scored}) to
    # STATE. `state` was read BEFORE that, so writing it back verbatim would
    # clobber the cache stamp — and with the stamp gone the next run would
    # treat the cache as stale and recompute all 72 combos (a 3-minute cron)
    # every single day. Merge the cache back so the stamp survives the write.
    try:
        disk = json.loads(STATE.read_text())
        if "cost_rt" in disk:
            state["cost_rt"] = disk["cost_rt"]
        if "scored" in disk:
            state["scored"] = disk["scored"]
        if "input_sig" in disk:
            state["input_sig"] = disk["input_sig"]
    except Exception:
        pass
    if not best:
        print("paper-learner: not enough data / no config beat threshold")
        return
    score, dip, rsi_max, red_min, ev, n, acc = best
    print(f"  best found (NET of fees): dip={dip} rsi<={rsi_max} "
          f"red>={red_min} acc={acc:.1f}% net_ev={ev:+.3f}% n={n}")
    # ── RECENCY (2026-10-20) ───────────────────────────────────────────────
    # A full-sample EV says nothing about whether the selected config still
    # FIRES. The config this learner adopted before the DB fix (-0.08/30/4)
    # scored net_ev=+0.184% on the frozen file and fired ZERO times in the
    # last 90 days — a great number about a market that no longer existed.
    # Re-derive the same two facts for the config just selected, every run
    # (rule 9), so an untestable winner cannot be adopted silently again.
    recent_n = recent_ev = 0
    try:
        series_cache = _load_all_series()
        evs, ns = [], []
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        cutoff = now_ms - 90 * 86400_000
        for key, closes in series_cache.items():
            tf = key.split(":", 1)[0]
            hold = HOLDS[tf]
            win = _LOAD_WINDOW.get(key)
            if not win or not closes:
                continue
            span = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000}[tf]
            start_idx = max(0, int((cutoff - win[0]) // span))
            sub = closes[start_idx:]
            r = simulate(sub, dip, rsi_max, red_min, hold) if len(sub) > 120 else None
            if r:
                evs.append(r[2]); ns.append(r[0] + r[1])
        recent_n = sum(ns)
        recent_ev = sum(e * k for e, k in zip(evs, ns)) / recent_n if recent_n else 0.0
    except Exception:  # noqa: BLE001 — a reporting line must never kill the run
        recent_n = 0
    if recent_n:
        print(f"  RECENCY: same config on the last 90 days — n={recent_n} "
              f"net_ev={recent_ev:+.3f}%")
    else:
        print("  RECENCY: same config fired ZERO times in the last 90 days "
              "(untestable in the time available — do not size on it)")
    # Judge on what the config will ACTUALLY get, not on what it did in a
    # crash-heavy full sample: a winner that does not trade cannot be
    # validated in the time available, so it is flagged rather than adopted.
    recency_ok = recent_n >= 20
    # ── INPUT PROVENANCE (2026-10-20) ──────────────────────────────────────
    # The grid is scored on FROZEN sweep files (data/live_fine.json and
    # alts_fine.json stop at 2026-08-15), while the live DB carries candles
    # to today. The verdict line used to say only "best found … n=907",
    # which reads as a statement about the CURRENT market. It is not: it is
    # a statement about 2023-01-01..2026-08-15. Downstream consumers
    # (self_improve, night_tuner) then reasoned about today from it.
    # Print the window and the age so no reader can mistake one for the other.
    # Never quoted as a constant — re-derived from the file each run (rule 9).
    if _LOAD_STAMP:
        try:
            srcs = {}
            for f, _ in _LOAD_STAMP.values():
                srcs[f] = srcs.get(f, 0) + 1
            parts = []
            for f, cnt in sorted(srcs.items()):
                if f == "trading.db":
                    # live store: name the WINDOW, not the age of the file
                    wins = [_LOAD_WINDOW[k] for k in _LOAD_WINDOW
                            if _LOAD_STAMP.get(k, ("",))[0] == f]
                    if wins:
                        lo = datetime.fromtimestamp(min(w[0] for w in wins) / 1000, timezone.utc)
                        hi = datetime.fromtimestamp(max(w[1] for w in wins) / 1000, timezone.utc)
                        age_h = (datetime.now(timezone.utc) - hi).total_seconds() / 3600
                        parts.append(f"{f} x{cnt} ({lo:%Y-%m-%d}..{hi:%Y-%m-%d %H:%M}, "
                                     f"latest bar {age_h:.1f}h old)")
                else:
                    mts = [mt for k, (ff, mt) in _LOAD_STAMP.items() if ff == f and mt]
                    if mts:
                        age_d = (datetime.now(timezone.utc).timestamp() - min(mts)) / 86400
                        parts.append(f"{f} x{cnt} (frozen {age_d:.0f}d ago)")
            if parts:
                print(f"  INPUT: {len(_LOAD_STAMP)} series from " + "; ".join(parts))
        except Exception:
            pass
    # Adopt only if it improves NET expectancy meaningfully over the current
    # config. The old rule compared ACCURACY (+1.5pp) — which adopts a config
    # for being right more often, not for earning more, the documented
    # failure mode. Expectancy is what compounds, so gate on it.
    # (out-of-sample guard is implicit: the sweep is historical, live is unseen)
    cur_ev = current.get("ev_net")
    if cur_ev is None:
        # No comparable net figure for the current config (legacy gross state).
        # Adopt the net-best cleanly and label the transition honestly.
        adopt = True
        reason = "current had no comparable net figure (legacy gross state)"
    elif ev >= cur_ev + 0.05:      # require >= 0.05pp net-EV lift, not noise
        adopt = True
        reason = f"net_ev {cur_ev:+.3f}% -> {ev:+.3f}%"
    else:
        adopt = False
        reason = f"best net_ev {ev:+.3f}% not >= current {cur_ev:+.3f}% + 0.05pp"
    if adopt and not recency_ok:
        # The full-sample EV cleared the bar but the config barely trades. It
        # is not wrong, it is untestable: ~10 trades/yr cannot be validated by
        # a daily learner and cannot pay on this account size. Record the
        # measurement, say why it was not adopted.
        adopt = False
        reason = ("best config is untestable — it fired %d times in 90 days "
                  "(full-sample EV %+.3f%% describes a crash-heavy past, not "
                  "the live regime); not adopted"
                  % (recent_n, ev))
        state["untestable_best"] = {
            "dip": dip, "rsi_max": rsi_max, "red_min": red_min,
            "acc": round(acc, 1), "ev_net": round(ev, 3), "n": n,
            "recent_n_90d": recent_n, "recent_ev_net": round(recent_ev, 3),
            "utc": datetime.now(timezone.utc).isoformat()}
    if adopt:
        state["current_params"] = {"dip": dip, "rsi_max": rsi_max,
                                    "red_min": red_min, "acc": round(acc, 1),
                                    "ev_net": round(ev, 3), "n": n,
                                    "adopted_utc": datetime.now(timezone.utc).isoformat()}
        state["history"] = state.get("history", [])[-20:] + [{
            "utc": datetime.now(timezone.utc).isoformat(),
            "dip": dip, "rsi_max": rsi_max, "red_min": red_min,
            "acc": round(acc, 1), "ev_net": round(ev, 3), "n": n}]
        STATE.write_text(json.dumps(state, indent=2))
        print(f"paper-learner: ADOPTED new params ({reason})")
        # INERTNESS (2026-10-07): these params are read by NO trader —
        # unified_trader.py holds its own hard-coded validated buckets. Say so
        # loudly: an "ADOPTED" line that nothing consumes reads as progress in
        # the log, which is the exact failure check-improvement-consumed.py
        # exists to catch. This learner's output is a SIGNAL, not a control.
        print("paper-learner: NOTE — adopted params are recorded for analysis "
              "only; no live/paper trader reads paper-learner-state.json "
              "(unified_trader holds its own buckets).")
    else:
        # Persist EITHER way: the untestable_best record written above, and
        # the adopted flag clearing, must survive this run.
        try:
            state.setdefault("cost_rt", FEE_RT)
            STATE.write_text(json.dumps(state, indent=2))
        except Exception:  # noqa: BLE001
            pass
        print(f"paper-learner: kept current ({reason})")


if __name__ == "__main__":
    main()
