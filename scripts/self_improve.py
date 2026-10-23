#!/usr/bin/env python3
"""SELF-IMPROVEMENT ENGINE — zero-token autonomous optimizer (2026-09-12).

Runs on system crontab (pure Python, no LLM) so it can NEVER fail on API
balance like the old agent-based job did (HTTP 402).

Each run it:
  1. Loads all performance data (live, paper, futures, gold).
  2. Measures per-edge / per-symbol stats.
  3. Runs a parameter search on the core signal (stop / trail / regime) over a
     4-point grid, split into an in-sample and an out-of-sample half, and
     compares EXPECTANCY PER TRADE. Both halves must be positive after fees.

     ⚠️ CORRECTED 2026-09-23. This step used to be described as "out-of-sample
     validated ... picks the best config that generalises", and it was neither.
     Two defects, both measured on the live data:

       (a) The halves were compared as SUMS of per-trade returns. Sums scale
           with trade count, so the ranking mostly ranked configs by how many
           trades they took, not by edge quality.

       (b) The in-sample half produced ZERO trades on all 5 symbols (OOS: 72).
           `sum([])` is 0, so the gate `isp >= -1.0` could never fail. The
           config was therefore selected on the exact half it calls
           out-of-sample — pure selection on the test set.

     An empty half now FAILS CLOSED: no config is applied, and the report says
     why. Tuning is refused rather than guessed. On current data that means no
     parameter change is applied at all, which is the honest outcome.
  4. Applies only SAFE, BOUNDED changes (hard-clamped envelope below).
  5. Disables proven-losing edges and flags promotion candidates in a
     signals-state file (never patches credentials or code blindly).
  6. Writes reports/self-improve.json and pings Telegram with a summary.
     A failed notification is now RECORDED (notified: false + notify_error)
     rather than silently swallowed, because the card IS the delivery.

HARD ENVELOPE (never exceeded):
  stop 0.008-0.030 | tp_rr 1.2-3.0 | max_open 2 | size <= 7.0
  daily loss cap >= -3.0 (spot) | futures leverage == 3 (never touched)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paper_net import summarise as paper_summarise, per_symbol_net, net_of as paper_net_of  # noqa: E402
try:
    from paper_book import collapse_multitf as _collapse_multitf
except Exception:  # noqa: BLE001 — never let the reader kill the loop
    _collapse_multitf = None
try:
    from paper_book import edge_liveness as _edge_liveness
except Exception:  # noqa: BLE001 — never let the reader kill the loop
    _edge_liveness = None

# An edge with no fill for this long is NOT LIVE: its per-edge record is a
# frozen population, so it can neither be promoted nor told to tighten. Two
# weeks is ~4x the native opener's worst observed quiet gap in the live book.
LIVE_DAYS = 14.0


def _filled_since(ts, days: float) -> bool:
    """True when `ts` is inside `days`. An unstamped/odd stamp is NOT live:
    the caller only asks this about an edge it wants to act on, and acting on
    an edge whose last trade cannot be dated is what this guard exists to
    stop. (The read-side `improvement_loop._edge_is_live` is deliberately the
    other way round — it must not silence an edge it cannot date.)"""
    import datetime as _dt_mod
    try:
        _dt = _dt_mod.datetime
        dt = _dt.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt_mod.timezone.utc)
    except (TypeError, ValueError):
        return False
    return (_dt.now(_dt_mod.timezone.utc) - dt).total_seconds() <= days * 86400

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
REPORTS = BASE / "reports"
LOGS = BASE / "logs"
ENV = BASE / "config" / "live-small-account.env"
STATE = DATA / "signals-state.json"

# --- hard envelope (never exceed) ---
BOUNDS = {
    "LIVE_STOP_PCT": (0.008, 0.030),
    "LIVE_TP_RR": (1.2, 3.0),
    "LIVE_MAX_OPEN": (1, 2),
    "LIVE_SIZE_USD": (3.0, 7.0),
}
FEE = 0.002


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(p: Path, default):
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        # Loud on purpose. A malformed or unreadable state file previously
        # returned the caller's default and read downstream as "no data", so
        # the engine would quietly optimise on less than it believed it had.
        print(f"  ⚠️ could not read {p.name}: {type(e).__name__}: {e}",
              file=sys.stderr)
    return default


def read_env() -> dict:
    out = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


# REMOVED 2026-09-23: write_env().
#
# It was defined here and NEVER CALLED — not in this file, not anywhere in the
# repo. It parsed config/live-small-account.env and rewrote the keys it managed,
# which is a real job that something else now does:
#
#     night_tuner.py  →  LIVE_STOP_PCT in live-small-account.env
#                        (bounded ±0.002/night, floor 0.008, ceiling 0.030)
#
# The env file still carries a comment from the era when this function WAS wired
# up ("auto-improve pass ... tightened 0.011 by 0.002 per improvement-pass rule"),
# so the responsibility moved to night_tuner and left this behind.
#
# Dead code that looks like a live control path is worse than no code: a reader
# auditing "what can change my live stop loss?" would find this and conclude the
# self-improver owns it. It does not.


def stats(trades: list, pnl_key: str = "pnl") -> dict:
    n = len(trades)
    if not n:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "ev": 0.0}
    w = sum(1 for t in trades if (t.get(pnl_key) or 0) > 0)
    pnl = sum((t.get(pnl_key) or 0) for t in trades)
    return {"n": n, "wr": w / n * 100, "pnl": pnl, "ev": pnl / n}


def stats_net_of(trades: list) -> dict:
    """NET-of-cost stats for a set of paper trades — the disable/promote basis.

    WHY (2026-10-13): the per-edge disable gate below WRITES the list that
    `unified_trader` honours on the real-money path, but it read GROSS `pnl`.
    With the measured round trip at ~0.16%, an edge whose true edge is thinner
    than the fee was kept alive by the flattered figure: edge 'loose' reads
    +0.14 gross (kept) but -0.35 net (a loser). This is the exact class of
    silent-unit bug the improvement layer was fixed for on 2026-10-11; this
    reader is the same fix, applied to the one gate that was missed.

    Returns {'n', 'wr', 'pnl', 'ev', 'gross', 'wr_gross'} where 'wr'/'pnl'/'ev'
    are NET and 'gross'/'wr_gross' are carried so the haircut stays visible.
    'wr' counts NET wins (it always did here); 'wr_gross' is the gross rate the
    report used to quote for the whole book, labelled so the two cannot be
    confused (2026-10-18 — the same mixed-units defect was still live in
    improvement_loop's per-edge table and night_tuner's net branch).
    """
    n = len(trades)
    if not n:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "ev": 0.0, "gross": 0.0,
                "wr_gross": 0.0}
    nets = [paper_net_of(t)[0] for t in trades]
    gross = sum((t.get("pnl") or 0) for t in trades)
    w = sum(1 for x in nets if x > 0)
    w_gross = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
    net = sum(nets)
    return {"n": n, "wr": w / n * 100, "pnl": net, "ev": net / n,
            "gross": gross, "wr_gross": w_gross / n * 100}


def _should_disable(st: dict) -> bool:
    """The per-edge disable predicate, extracted so the last-edge guard and the
    disable loop read ONE definition and cannot drift apart.

    Rule (unchanged since 2026-10-13, on the NET basis): an edge with a real
    sample (n>=15) that wins less than 42% of the time and loses money net of
    the round trip is a proven loser. NOTE this predicate alone is NOT safe to
    apply to every edge — every edge is a net loser after the measured fee, so
    the CALLER must additionally guarantee it never disables the last open edge
    (see `_plan_disables`, and the 2026-10-14 freeze)."""
    return st["n"] >= 15 and st["wr"] < 42 and st["pnl"] < 0


def _plan_disables(by_edge: dict, already_disabled) -> tuple[list, str | None]:
    """Decide which edges this run may disable — with a FLOOR, not just a rule.

    Returns (to_disable, survivor). `to_disable` are the edges the predicate
    flags and that are not already disabled; `survivor` is the strongest flagged
    edge held back so the paper book can never be emptied (None when the guard
    did not need to fire).

    WHY (2026-10-14): `_should_disable` is TRUE for essentially every edge once
    the ~0.16% round trip is charged, so applying it to the whole set disabled
    ALL EIGHT edges — including the native openers 'loose' + 'validated'. Every
    opener honours the list, so the book stopped opening: unified-paper.json
    froze at 2026-10-12T18:16 while the scan fired signals into the void. A
    disable is only safe while something REMAINS to carry the book, so if the
    rule would leave zero enabled, keep the strongest candidate (highest net,
    then n — closest to viable) and refuse the rest.
    """
    disabled = set(already_disabled or [])
    flagged = [e for e, ts in by_edge.items()
               if e not in disabled and _should_disable(stats_net_of(ts))]
    remaining = [e for e in by_edge if e not in disabled and e not in flagged]
    if not flagged or remaining:
        return flagged, None  # guard not needed — something stays enabled
    flagged.sort(key=lambda e: (stats_net_of(by_edge[e])["pnl"],
                                stats_net_of(by_edge[e])["n"]))
    return flagged, flagged.pop()  # keep the strongest; the rest still go


def _identity(t: dict) -> tuple:
    """A stable identity for a closed trade, so `dedupe` removes only TRUE
    re-bookings and never two distinct trades.

    🔴 WHY (2026-10-14, overnight shift): the old key was
    `("symbol", "entry", "exit", "close_ts_utc")`, truncated to 16 chars each.
    On the PAPER book that is a catastrophe: paper records carry `exit=None`
    and `close_ts_utc=None` (only 2 of 2941 records set them), so the key
    DEGENERATES TO `(symbol, entry)` — every trade ever opened at the same
    price on a symbol, months apart, with different TP/SL and different
    outcomes, collapsed into ONE. Measured: `paper_c` came out at n=2229 when
    the book holds 2941 distinct closes — 712 real trades (24%) silently
    dropped, hiding +7.14 gross / ~+2.7 net. It flipped decisions: edge
    'momentum' read n=24 wr=29% net=-0.18 (DISABLE) truncated vs n=30 wr=40%
    net=+0.19 (keep) on the full set; 'rsi2' sat at n=5 (below the n>=15
    floor, never judged) vs n=27 wr=56% true.

    The lesson loop already uses the correct identity: symbol|ts_utc|entry|
    size|pnl (all fields present and 1:1 with a trade). This key mirrors that.
    `ts_utc` is the OPEN time and is present on every paper record; a trade
    identity must include an opening timestamp or same-price re-entries merge.
    """
    ts = t.get("open_ts_utc") or t.get("ts_utc") or ""
    size = t.get("size", t.get("size_usdt"))
    return (t.get("symbol"), ts, str(t.get("entry")), str(size))


def dedupe(trades: list, keys=None) -> list:
    """Remove TRUE duplicate re-bookings (same trade written twice).

    Default identity is `_identity` (symbol + open time + entry + size). A
    caller may still pass explicit `keys`; if it does, they are used verbatim
    WITHOUT truncation (the 16-char truncation of an ISO timestamp collapsed
    records to minute resolution and was part of the same defect).
    """
    seen = set()
    out = []
    for t in trades:
        k = _identity(t) if keys is None else tuple(
            str(t.get(x, "")) for x in keys)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


# ---------------------------------------------------------------- optimizer
def optimize(symbols: list[str]) -> dict:
    """OOS-validated search over stop/trail on the sharp dip signal.
    Returns the best config that is positive in BOTH halves, else current."""
    try:
        sys.path.insert(0, str(BASE / "scripts"))
        from trading.exchange.live_client import make_live_exchange
        ex = make_live_exchange()
    except Exception as e:
        # 🔴 This used to be a bare `return {}`, which is INDISTINGUISHABLE from
        # "the search ran and found no config better than the current one" —
        # both produce {} and both end as "kept current" in the report. An
        # exchange outage therefore read as a completed, healthy pass.
        #
        # Now it says what actually happened, so a skipped optimizer cannot be
        # mistaken for a search that found nothing.
        print(f"  ⚠️ optimizer SKIPPED — exchange unavailable: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return {"skipped": f"exchange unavailable: {type(e).__name__}"}

    def rsi(c, n=14):
        if len(c) < n + 1:
            return 50.0
        g = l = 0.0
        for i in range(-n, 0):
            dx = c[i] - c[i - 1]
            g += dx if dx > 0 else 0
            l += -dx if dx <= 0 else 0
        return 100.0 if l == 0 else 100 - 100 / (1 + (g / n) / (l / n))

    def ema(cl, n=200):
        k = 2 / (n + 1)
        e = cl[0]
        for v in cl[1:]:
            e = v * k + e * (1 - k)
        return e

    def sim(o, sl, trail, act, use_regime):
        cl = [x[4] for x in o]
        hi = [x[2] for x in o]
        out = []
        for i in range(210, len(o) - 97):
            c = cl[:i + 1]
            px = c[-1]
            r = rsi(c)
            dd = px / max(hi[i - 90:i]) - 1
            if not (dd <= -0.011 and r <= 35):
                continue
            if use_regime and px < ema(c, 200):
                continue
            e = cl[i]
            stop = e * (1 - sl)
            peak = e
            ret = None
            for b in o[i + 1:i + 97]:
                if b[3] <= stop:
                    ret = stop / e - 1
                    break
                if b[2] > peak:
                    peak = b[2]
                if peak >= e * (1 + act):
                    stop = max(stop, peak * (1 - trail))
            if ret is None:
                ret = cl[i + 96] / e - 1
            out.append(ret - FEE)
        return out

    def _fetch_paginated(sym: str, pages: int = 3) -> list:
        """Fetch up to `pages`x1000 15m bars by walking backwards with `since`.

        🔴 ROOT-CAUSE FIX (2026-10-06): Binance caps a single fetch_ohlcv at
        1000 bars regardless of the requested limit. The old code asked for
        1200, got 1000, split it into two 500-bar halves, then spent the first
        200 bars of EACH half rebuilding the EMA200 and reserved the next 97
        for the forward window — leaving only ~193 usable iterations per half.
        The sharp-dip+RSI+regime gate is rare, so the OUT-OF-SAMPLE half
        routinely produced ZERO signals. With oos_n==0 the fail-closed guard
        below refused every config, so the optimizer could NEVER tune and
        reported "no config generalised" forever. Giving it more history is
        the fix; the two halves each get their own full 200-bar warmup.
        """
        base = ex.fetch_ohlcv(sym, "15m", limit=1000)
        if not base:
            return []
        merged = {x[0]: x for x in base}
        earliest = base[0][0]
        step = 1000 * 15 * 60 * 1000
        for _ in range(pages - 1):
            try:
                page = ex.fetch_ohlcv(sym, "15m", limit=1000,
                                      since=earliest - step)
            except Exception:
                break
            if not page:
                break
            for x in page:
                merged[x[0]] = x
            earliest = min(x[0] for x in page)
        return [merged[k] for k in sorted(merged)]

    data = {}
    failed = []
    for s in symbols:
        try:
            o = _fetch_paginated(s, pages=3)
            # Need >= 1000 bars so each half keeps a full 200-bar EMA warmup
            # PLUS a meaningful forward window. Below that the OOS half is a
            # coin flip on whether it produces any signal at all.
            if o and len(o) >= 1000:
                data[s] = o
            else:
                failed.append(f"{s}(short {len(o)})")
        except Exception as e:
            # A per-symbol fetch failure silently shrank the optimizer's sample.
            # The search then reported a confident OOS figure computed on fewer
            # symbols than intended, with nothing in the report to say so.
            failed.append(f"{s}({type(e).__name__})")
    if failed:
        print(f"  ⚠️ optimizer sample reduced — {len(failed)} symbol(s) "
              f"unusable: {', '.join(failed[:6])}"
              f"{' …' if len(failed) > 6 else ''}", file=sys.stderr)

    grid = [
        (0.012, 0.008, 0.008, True),
        (0.015, 0.010, 0.010, True),
        (0.010, 0.006, 0.006, True),
        (0.020, 0.012, 0.010, True),
    ]
    best = None
    results = []
    unvalidated = []
    for sl, trail, act, reg in grid:
        is_p, oos_p = [], []
        for s, o in data.items():
            half = len(o) // 2
            is_p += sim(o[:half], sl, trail, act, reg)
            oos_p += sim(o[half:], sl, trail, act, reg)

        # 🔴 MEANS, NOT SUMS.
        #
        # This used to be `isp = sum(is_p) * 100` and `score = isp + oosp`, i.e.
        # the SUM of per-trade returns. Sums scale with trade count, so ranking
        # by them mostly ranks configs by how many trades they took — a config
        # that trades twice as often with the same per-trade edge scores double.
        # Across a fixed 4-point grid that is not a small distortion; it is the
        # dominant term. Expectancy per trade is the comparable quantity.
        is_m = (sum(is_p) / len(is_p) * 100) if is_p else None
        oos_m = (sum(oos_p) / len(oos_p) * 100) if oos_p else None
        results.append({"sl": sl, "trail": trail, "regime": reg,
                        "is_pct": round(is_m, 3) if is_m is not None else None,
                        "oos_pct": round(oos_m, 3) if oos_m is not None else None,
                        "is_n": len(is_p), "oos_n": len(oos_p),
                        "n": len(is_p) + len(oos_p)})

        # 🔴 FAIL CLOSED on a half that produced no trades.
        #
        # Measured 2026-09-23: the in-sample half produced ZERO trades on all 5
        # symbols while the out-of-sample half produced 72. `sum([])` is 0, so
        # the old gate `isp >= -1.0` could NEVER fail — the config was therefore
        # selected purely on the half it calls out-of-sample. The docstring's
        # "picks the best config that generalises" and "require BOTH halves" were
        # false in practice: there was only ever one half.
        #
        # An empty half cannot validate anything, so it is excluded from
        # selection rather than silently passing.
        if is_m is None or oos_m is None:
            unvalidated.append((sl, trail, "IS" if is_m is None else "OOS",
                                len(is_p), len(oos_p)))
            continue

        # Both halves must be positive PER TRADE, after fees. Requiring merely
        # >= -1% (the old sum-based bar) accepted a losing configuration.
        if is_m > 0 and oos_m > 0:
            score = is_m + oos_m
            if best is None or score > best[0]:
                best = (score, sl, trail, act, reg, is_m, oos_m,
                        len(is_p) + len(oos_p))
    return {"best": best, "grid": results, "unvalidated": unvalidated}


# ---------------------------------------------------------------- main
def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    findings = []
    changes = []

    # 1) load performance
    live = load_json(DATA / "live-trades.json", {"open": [], "closed": []})
    paper = load_json(DATA / "unified-paper.json", {"open": [], "closed": []})
    fut = load_json(DATA / "futures-trades.json", {"open": [], "closed": []})

    # Identity-based dedupe (2026-10-14). The previous key
    # ("symbol","entry","exit","close_ts_utc") degenerates on the PAPER book
    # (exit/close_ts_utc are None there) to (symbol, entry) and dropped 712 of
    # 2941 real trades. `dedupe` now defaults to a stable symbol+open-time+
    # entry+size identity that only collapses true re-bookings.
    live_c = dedupe(live.get("closed", []))
    paper_c = dedupe(paper.get("closed", []))

    # MULTI-TIMEFRAME COLLAPSE (2026-10-15). `dedupe` removes byte-identical
    # re-bookings; it cannot touch a fill written once PER TIMEFRAME, because
    # those copies differ in `ts_utc` (sub-second) and often in `entry` by one
    # tick, so each is a different key and all survive. Measured: 498 such pairs
    # (476 cross-tf, 22 same-tf microsecond double-writes), all sharing an exact
    # entry, an exact pnl and an exact exit reason. They inflated every per-edge
    # / per-symbol sample by ~17% and double-counted $5.01 gross / ~$1.4 net.
    # Collapse them here so the gates below read one row per physical trade.
    multi_tf_removed = 0
    if _collapse_multitf is not None:
        paper_c, multi_tf_removed = _collapse_multitf(paper_c)
    else:
        # Same rule as improvement_loop: a reader that silently skips the
        # collapse double-counts every fill once per timeframe, and the per-edge
        # gates below then act on inflated samples. Say so on the card.
        findings.append(
            "⚠️ MULTI-TF COLLAPSE UNAVAILABLE — per-edge samples below count "
            "one fill once per timeframe (5m/15m/30m); do not act on them.")

    fut_c = fut.get("closed", [])

    ls, ps, fs = stats(live_c), stats(paper_c), stats(fut_c)
    # The paper book charges costs (2026-10-06) and records pnl_net. Report the
    # NET figure — the gross one was what the card quoted, and it is flattered by
    # exactly the costs the book now measures. Gross is kept in parens for the
    # reader who wants to see the haircut.
    pnet = paper_summarise(paper_c)
    findings.append(f"live: n={ls['n']} wr={ls['wr']:.0f}% pnl={ls['pnl']:+.3f}")
    findings.append(
        f"paper NET: n={pnet['n']} wr={pnet['wr_net']:.0f}% "
        f"pnl={pnet['net_pnl']:+.3f} ev/trade={pnet['ev_net']:+.5f} "
        f"(gross {pnet['gross_pnl']:+.3f}, wr {pnet['wr_gross']:.0f}%)"
        + (f"; {pnet['uncosted']} record(s) uncosted" if pnet['uncosted'] else ""))
    findings.append(f"futures: n={fs['n']} wr={fs['wr']:.0f}% pnl={fs['pnl']:+.3f}")
    # Surface the multi-timeframe collapse so a shrinking book is never silent.
    if multi_tf_removed:
        findings.append(
            f"paper MULTI-TF COLLAPSE: removed {multi_tf_removed} duplicate "
            f"record(s) — one fill booked once per timeframe (5m/15m/30m). "
            f"Every per-edge sample below is on the collapsed book.")

    # 1b) per-symbol net edge — which symbols actually EARN after costs.
    #     A symbol whose net is negative loses money at any win rate; the owner
    #     should see that rather than the aggregate, which hides it. Only the
    #     biggest earners/losers are named so the card stays short.
    psn = per_symbol_net(paper_c)
    ranked = sorted(psn.items(), key=lambda kv: kv[1]["net"], reverse=True)
    ranked = [(s, d) for s, d in ranked if d["n"] >= 20]   # ignore tiny samples
    if ranked:
        top = ranked[0]
        findings.append(
            f"paper best symbol: {top[0]} n={top[1]['n']} "
            f"net={top[1]['net']:+.3f} (gross {top[1]['gross']:+.3f})")
        losers = [(s, d) for s, d in ranked if d["net"] < 0]
        if losers:
            names = ", ".join(f"{s} {d['net']:+.2f}" for s, d in losers[:4])
            findings.append(
                f"paper net-LOSING symbols (n>=20): {len(losers)} — {names}")

    # 2) optimizer — OOS validated parameter search
    opt = optimize(["SOL/USDT", "XRP/USDT", "ETH/USDT", "BTC/USDT", "LINK/USDT"])
    if opt.get("best"):
        score, sl, trail, act, reg, isp, oosp, n = opt["best"]
        findings.append(
            f"optimizer: best stop={sl:.3f} trail={trail:.3f} "
            f"IS={isp:+.1f}% OOS={oosp:+.1f}% (n={n}) — generalises")
        # apply to the futures executor constants if materially better & safe
        try:
            import re
            fp = BASE / "scripts" / "futures_executor.py"
            src = fp.read_text(encoding="utf-8")
            new = src
            new = re.sub(r"^STOP_PCT = [0-9.]+.*$",
                         f"STOP_PCT = {sl}     # auto-tuned {now()[:10]} (OOS {oosp:+.1f}%)",
                         new, count=1, flags=re.M)
            new = re.sub(r"^TRAIL_DIST = [0-9.]+.*$",
                         f"TRAIL_DIST = {trail}      # auto-tuned {now()[:10]}",
                         new, count=1, flags=re.M)
            new = re.sub(r"^TRAIL_ACTIVATE = [0-9.]+.*$",
                         f"TRAIL_ACTIVATE = {act}  # auto-tuned {now()[:10]}",
                         new, count=1, flags=re.M)
            if new != src:
                # verify syntax before writing
                import py_compile
                tmp = fp.with_suffix(".tmp.py")
                tmp.write_text(new, encoding="utf-8")
                py_compile.compile(str(tmp), doraise=True)
                tmp.unlink()
                fp.write_text(new, encoding="utf-8")
                changes.append(f"futures stop={sl} trail={trail} act={act}")
        except Exception as e:
            findings.append(f"optimizer apply skipped: {str(e)[:60]}")
    else:
        # Distinguish the three genuinely different outcomes. All of them used to
        # print the same sentence ("no config generalised"), so an exchange
        # outage and an unvalidatable sample both read as a healthy no-change
        # pass.
        if opt.get("skipped"):
            findings.append(
                f"optimizer: SKIPPED — {opt['skipped']} (NOT a search result)")
        elif opt.get("unvalidated"):
            which = ", ".join(f"sl={s} trail={t}: {half} half had 0 trades "
                              f"(IS={a} OOS={b})"
                              for s, t, half, a, b in opt["unvalidated"])
            findings.append(
                f"optimizer: NO CHANGE — sample cannot validate any config "
                f"({which}). An empty half passes a >="
                f" check for free, so tuning is refused rather than guessed.")
        else:
            # The search ran on a real sample and every config was negative in
            # at least one half. Report WHICH and WHY, so a healthy "we measured
            # it and it loses" is distinguishable from an untested pass.
            grid = opt.get("grid", [])
            neg_both = [g for g in grid
                        if g.get("is_pct") is not None
                        and g.get("oos_pct") is not None
                        and g["is_pct"] <= 0 and g["oos_pct"] <= 0]
            if grid and len(neg_both) == len(grid):
                worst = min(grid, key=lambda g: (g["is_pct"] or 0) + (g["oos_pct"] or 0))
                findings.append(
                    f"optimizer: NO CHANGE — all {len(grid)} configs NEGATIVE in "
                    f"BOTH halves on real data (n={grid[0].get('n')}); the "
                    f"sharp-dip entry is negative-EV after fees, so no exit "
                    f"tuning can rescue it. Best-of-bad: sl={worst['sl']} "
                    f"trail={worst['trail']} IS={worst['is_pct']:+.2f}% "
                    f"OOS={worst['oos_pct']:+.2f}%. Entry signal needs "
                    f"rework, not tuning.")
            else:
                findings.append("optimizer: no config generalised (kept current)")

    # 3) disable proven-losing edges (safe: state file, not code)
    state = load_json(STATE, {"disabled": [], "promoted": []})
    by_edge = {}
    for t in paper_c:
        e = t.get("edge")
        if e and e != "?":
            by_edge.setdefault(e, []).append(t)
    # 🔴 NEVER DISABLE THE LAST OPEN EDGE (2026-10-14).
    #
    # Found 2026-10-14: over several nightly runs this step disabled EVERY edge
    # it had ever seen — 'pullback', 'supertrend', 'ema-cross', 'bollinger',
    # 'momentum', 'breakout' AND the two ACTIVE openers 'loose' + 'validated'
    # (log: "APPLIED: disabled edge 'loose'" then "disabled edge 'validated'").
    # The disable list is honoured by every opener (the 2026-10-12 fix routed
    # all of them through `_edge_disabled`), so once 'validated' and 'loose'
    # were on it the paper book STOPPED OPENING ENTIRELY: unified-paper.json
    # froze at 2026-10-12T18:16 and the live scan has since fired hundreds of
    # signals straight into the void ("paper signals fired=14", "=8", "=3" with
    # zero PAPER OPEN lines). The learning track was dead for ~1.5 days while
    # this engine kept reporting metrics and the improvement loop kept
    # "analysing" a frozen book as if it were current.
    #
    # `_plan_disables` applies the rule WITH A FLOOR: it never returns a plan
    # that leaves zero enabled edges (see its docstring for the full rationale).
    to_disable, survivor = _plan_disables(by_edge, state["disabled"])

    # LIVENESS (2026-10-21): which edges still produce fills, and which are
    # copies of another edge's handoff. Both gates below (disable, promote) are
    # otherwise applied to an UNBOUNDED record — the same "a recorded number is
    # a measurement, re-read it" rule as rule #13, applied to time.
    liveness = _edge_liveness(paper_c) if _edge_liveness is not None else {}
    last_fill = (liveness or {}).get("last_fill", {})
    redundant = (liveness or {}).get("redundant", {})
    for e, ts in sorted(last_fill.items()):
        if not _filled_since(ts, LIVE_DAYS):
            findings.append(
                f"edge '{e}' NOT LIVE: last fill {str(ts)[:10]} "
                f"(>{LIVE_DAYS:.0f}d) — its per-edge record is frozen; no "
                f"promote/decay verdict on it until it fills again")
    if redundant:
        findings.append(
            "redundant edge(s): "
            + "; ".join(f"'{e}' ~ '{k}'" for e, k in sorted(redundant.items()))
            + " — shared handoff, no better net/WR; not independent evidence")
    if survivor is not None:
        findings.append(
            f"REFUSED to disable the last open edge '{survivor}': disabling it "
            f"would leave ZERO enabled edges and freeze the paper book. Keeping "
            f"the strongest candidate; the other {len(to_disable)} still go.")

    for e, ts in by_edge.items():
        # Judge on NET, not gross (2026-10-13). Same class of defect the
        # improvement layer was fixed for on 2026-10-11 — but this gate, which
        # WRITES the disable list that unified_trader honours on the real-money
        # path, was left reading gross `pnl`. At gross, edge 'loose' reads
        # +0.14 (kept); at net it is -0.35 (a loser). The measured round trip is
        # 0.16%, so any edge whose true edge is thinner than the fee was being
        # kept alive by the flattered figure. Gross is carried in the message so
        # the haircut stays visible; the VERDICT reads net.
        st = stats_net_of(ts)
        if e in to_disable:
            state["disabled"].append(e)
            changes.append(
                f"disabled edge '{e}' (n={st['n']} wr={st['wr']:.0f}% "
                f"net={st['pnl']:+.2f}, gross {st['gross']:+.2f})")
        elif st["n"] >= 10 and st["wr"] >= 60 and st["pnl"] > 0:
            # LIVENESS GATE (2026-10-21): "proven" must mean proven WHILE
            # TRADING. A disabled/crashed edge keeps its record forever, and a
            # frozen mean over 134 fills looks exactly like a live one — so a
            # promotion decided on it would be promoting a memory. The same
            # reason the loop refuses a split-point it cannot date.
            lf = last_fill.get(e, "")
            if not _filled_since(lf, LIVE_DAYS):
                changes.append(
                    f"REFUSED to promote '{e}': last fill {str(lf)[:10] or '?'} "
                    f"(>{LIVE_DAYS:.0f}d ago) — the record is frozen, not "
                    f"evidence it still pays today")
            elif e in redundant:
                changes.append(
                    f"REFUSED to promote '{e}': redundant copy of "
                    f"'{redundant[e]}' — its evidence is the parent's, not a "
                    f"second proof")
            elif e not in state["promoted"]:
                state["promoted"].append(e)
                findings.append(
                    f"promotion candidate '{e}' (n={st['n']} wr={st['wr']:.0f}% "
                    f"net={st['pnl']:+.2f})")
    # FAIL LOUD if the book is already frozen (belt-and-braces): if every edge
    # is disabled the openers can never fire again, and no metric this engine
    # reports afterwards is a measurement of anything. Surface it as a change
    # so it lands on the card rather than in a log nobody reads.
    if by_edge and all(e in state["disabled"] for e in by_edge):
        changes.append(
            "WARNING: EVERY edge is disabled — the paper book cannot open and "
            "all per-edge metrics are frozen. Re-enable an edge in "
            "data/signals-state.json to resume learning.")
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # 4) write report
    report = {"ts": now(), "findings": findings, "changes": changes,
              "metrics": {"live": ls, "paper": ps, "futures": fs},
              "optimizer": opt.get("grid", [])}
    (REPORTS / "self-improve.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    # 5) telegram summary
    body = "\n".join(f"• {f}" for f in findings)
    chg = "\n".join(f"• {c}" for c in changes) if changes else "• none (no change for change's sake)"
    notified = False
    notify_err = None
    try:
        sys.path.insert(0, str(BASE / "src"))
        from trading.tg.bot_ui import send_card
        send_card("reports", "🔧 SELF-IMPROVE",
                  f"<b>Autonomous pass complete</b>\n{body}\n\n"
                  f"<b>Applied:</b>\n{chg}")
        notified = True
    except Exception as e:
        # 🔴 A silent failure here is the worst one in this file: the Telegram
        # card IS the delivery mechanism. If it fails quietly, the pass runs,
        # changes trade parameters, and the owner is told nothing — which is
        # indistinguishable from the engine having died.
        notify_err = f"{type(e).__name__}: {e}"
        print(f"  ⚠️ TELEGRAM NOTIFY FAILED — pass completed without "
              f"reporting: {notify_err}", file=sys.stderr)
        print(f"     findings that could not be delivered: {len(findings)}",
              file=sys.stderr)
        print(f"     changes that could not be delivered: {len(changes)}",
              file=sys.stderr)

    # record whether the summary actually left the box, so "the owner was not
    # told" is a recorded fact rather than an absence
    try:
        rep2 = json.loads((REPORTS / "self-improve.json").read_text())
        rep2["notified"] = notified
        if notify_err:
            rep2["notify_error"] = notify_err
        (REPORTS / "self-improve.json").write_text(
            json.dumps(rep2, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️ could not record notification status: "
              f"{type(e).__name__}: {e}", file=sys.stderr)

    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}] SELF-IMPROVE")
    for f in findings:
        print(f"  {f}")
    for c in changes:
        print(f"  APPLIED: {c}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"self-improve error: {str(e)[:200]}")
