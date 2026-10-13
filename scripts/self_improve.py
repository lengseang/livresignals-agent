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

    Returns {'n', 'wr', 'pnl', 'ev', 'gross'} where 'wr'/'pnl'/'ev' are NET and
    'gross' is carried so the haircut stays visible in the report.
    """
    n = len(trades)
    if not n:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "ev": 0.0, "gross": 0.0}
    nets = [paper_net_of(t)[0] for t in trades]
    gross = sum((t.get("pnl") or 0) for t in trades)
    w = sum(1 for x in nets if x > 0)
    net = sum(nets)
    return {"n": n, "wr": w / n * 100, "pnl": net, "ev": net / n,
            "gross": gross}


def dedupe(trades: list, keys) -> list:
    seen = set()
    out = []
    for t in trades:
        k = tuple(str(t.get(x, ""))[:16] for x in keys)
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

    live_c = dedupe(live.get("closed", []), ("symbol", "entry", "exit", "close_ts_utc"))
    paper_c = dedupe(paper.get("closed", []), ("symbol", "entry", "exit", "close_ts_utc"))
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
        if st["n"] >= 15 and st["wr"] < 42 and st["pnl"] < 0:
            if e not in state["disabled"]:
                state["disabled"].append(e)
                changes.append(
                    f"disabled edge '{e}' (n={st['n']} wr={st['wr']:.0f}% "
                    f"net={st['pnl']:+.2f}, gross {st['gross']:+.2f})")
        elif st["n"] >= 10 and st["wr"] >= 60 and st["pnl"] > 0:
            if e not in state["promoted"]:
                state["promoted"].append(e)
                findings.append(
                    f"promotion candidate '{e}' (n={st['n']} wr={st['wr']:.0f}% "
                    f"net={st['pnl']:+.2f})")
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
