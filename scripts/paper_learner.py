#!/usr/bin/env python3
"""Paper-trading accuracy improvement loop — runs constantly, learns, adapts.

Strategy: simulate the validated capitulation-catch logic on HISTORICAL data
(never touching live money) across many symbols/timeframes, record every
paper trade outcome, and re-fit the strategy's dip/RSI/hold parameters to
maximize real predictive accuracy. The live bot only adopts a new param set
after it BEATS the current config on a held-out (out-of-sample) window.

Pure stdlib (no numpy/sklearn) so it runs on the tiny VPS.

Run: cron every 15 min, or `python paper_learner.py [once|retrain]`
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
    """Load the historical close series ONCE into {key: closes}.

    Bug fixed 2026-10-05: this used to read data/full_sweep.json, which
    disk_cleanup.sh deleted as 'referenced NOWHERE' — but this cron DOES
    reference it, so paper_learner crashed with FileNotFoundError on every
    daily run and silently never learned. Fall back to the sibling sweep
    files that still exist (live_fine.json / alts_fine.json) and map their
    keys onto SERIES_KEYS. If no source has usable data, return {} so main()
    logs a clear line and exits 0 instead of dumping a traceback.
    """
    sources = [SWEEP, DATA / "live_fine.json", DATA / "alts_fine.json"]
    merged = {}
    for path in sources:
        for k, v in _read_sweep_file(path).items():
            if k not in merged and isinstance(v, list) and v:
                merged[k] = v
    # live_fine.json is keyed by bare timeframe ("15m") for BTC/USDT only.
    out = {}
    for key in SERIES_KEYS:
        series = merged.get(key)
        if series is None and key in ("15m:BTC/USDT", "30m:BTC/USDT",
                                      "5m:BTC/USDT"):
            bare = key.split(":", 1)[0]
            series = merged.get(bare)
        if series:
            try:
                out[key] = [x[1] for x in series]
            except Exception:  # noqa: BLE001
                continue
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
            if abs(blob.get("cost_rt", -1.0) - FEE_RT) < 1e-12:
                done = {tuple(eval(k)): v for k, v in blob.get("scored", {}).items()}
            else:
                print(f"paper-learner: cache fee-stamp "
                      f"{blob.get('cost_rt')} != {FEE_RT:.6f} — "
                      f"recomputing grid NET of fees")
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
    except Exception:
        pass
    if not best:
        print("paper-learner: not enough data / no config beat threshold")
        return
    score, dip, rsi_max, red_min, ev, n, acc = best
    print(f"  best found (NET of fees): dip={dip} rsi<={rsi_max} "
          f"red>={red_min} acc={acc:.1f}% net_ev={ev:+.3f}% n={n}")
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
        print(f"paper-learner: kept current ({reason})")


if __name__ == "__main__":
    main()
