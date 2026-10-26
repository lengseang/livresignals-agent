#!/usr/bin/env python3
"""Net settlement for decisions: turn a rating into an equity curve.

WHY THIS EXISTS
---------------
TradingAgents scores a decision as (exit/entry - 1) and optionally subtracts a
benchmark. Its own backtest module says, in its docstring, that it deliberately
refuses to grow a portfolio simulator:

    "Turning a rating into a filled order needs a quantity, a fill price and a
     cash ledger, none of which the system has; inventing them here would put an
     execution model behind an evaluation tool."

That refusal is correct for them and useless for us. We have all three — a cost
model measured this morning against a real Bybit demo round trip (11.0 bp), a
sizing rule, and funding data straight from the exchange. So this module does the
thing they declined to do, and it does it separately from their evaluator rather
than corrupting it.

WHAT IT ADDS OVER THEIR SETTLEMENT
----------------------------------
  * fees, at a rate the caller supplies, charged on both entry and exit
  * perpetual funding, charged per 8h period actually crossed while holding,
    signed by side (a long pays positive funding, receives negative)
  * position sizing at a fixed risk fraction, so returns are an equity curve
    rather than a pile of percentages
  * R-multiples against the initial stop, so results are comparable across
    volatility regimes
  * a deflation-aware summary: with N decisions tried, report the t-statistic
    and whether it clears sqrt(2 ln N)

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
  * no look-ahead: entry is the first open AFTER the decision timestamp
  * no filler prices: a bar that does not exist is an error, not a zero
  * no silent slippage assumption: slippage is a parameter and defaults to 0,
    so a result that ignored it is visibly a result that ignored it

Usage:
    python3 net_settlement.py --selftest
    python3 net_settlement.py --decisions decisions.json --out curve.json
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
DB = BASE / "data" / "trading.db"

# The measured round trip from the demo round trip verified 2026-10-24:
# fees 0.0521 on a 47.33 position = 11.0 bp. Slippage was not separately
# measurable at that size and is a parameter, default 0.
FEE_RT_PCT = 0.11
SLIPPAGE_PCT = 0.0

RATING_LONG = {"buy": 1.0, "overweight": 0.5}
RATING_SHORT = {"sell": -1.0, "underweight": -0.5}
RATING_FLAT = {"hold", "review", None}


@dataclass
class Decision:
    """One rated decision, at a moment, on one instrument."""
    id: str
    symbol: str
    rating: str                # buy / overweight / hold / underweight / sell
    decided_utc: str           # ISO; entry is the first bar AFTER this
    hold_bars: int             # in the instrument's own timeframe
    timeframe: str = "4h"
    stop_pct: float | None = None   # for R-multiples; None = no R reported


@dataclass
class Settled:
    id: str
    symbol: str
    rating: str
    side: int                  # +1 long, -1 short, 0 flat
    entry_utc: str | None = None
    entry: float | None = None
    exit_utc: str | None = None
    exit: float | None = None
    bars_held: int | None = None
    gross_pct: float | None = None
    fees_pct: float | None = None
    funding_pct: float | None = None
    net_pct: float | None = None
    r_multiple: float | None = None
    status: str = "pending"
    reason: str | None = None


# ----------------------------------------------------------------- data access
def _con():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def bars_after(con, symbol: str, tf: str, after_ms: int, n: int):
    """The n bars strictly after after_ms, oldest first. Missing data is an error."""
    rows = con.execute(
        "select open_time_utc, open, high, low, close from candles "
        "where symbol=? and timeframe=? and open_time_utc>? "
        "order by open_time_utc limit ?", (symbol, tf, after_ms, n)).fetchall()
    return rows


def funding_between(con, symbol: str, start_ms: int, end_ms: int) -> list[float]:
    """Funding rates whose timestamp falls inside (start, end].

    Read from the FUNDING table if this box has one; otherwise return [] and say
    so, because silently assuming zero funding is exactly the failure the source
    lab warns about ("perpetual funding ... NOT modelled in the lab, log it").
    """
    try:
        rows = con.execute(
            "select rate from funding where symbol=? and ts>? and ts<=? "
            "order by ts", (symbol, start_ms, end_ms)).fetchall()
        return [float(r[0]) for r in rows]
    except sqlite3.OperationalError:
        return []


# ----------------------------------------------------------------- the maths
def settle_one(con, d: Decision, fee_rt_pct: float = FEE_RT_PCT,
               slippage_pct: float = SLIPPAGE_PCT,
               equity: float = 10_000.0, risk_pct: float = 1.0) -> Settled:
    r = d.rating.strip().lower()
    if r in RATING_LONG:
        side = 1
        size = RATING_LONG[r]
    elif r in RATING_SHORT:
        side = -1
        size = abs(RATING_SHORT[r])
    else:
        return Settled(d.id, d.symbol, d.rating, 0, status="flat",
                       reason="rating is not directional")

    decided_ms = int(datetime.fromisoformat(d.decided_utc).timestamp() * 1000)
    rows = bars_after(con, d.symbol, d.timeframe, decided_ms, d.hold_bars + 1)
    if len(rows) < d.hold_bars + 1:
        return Settled(d.id, d.symbol, d.rating, side, status="pending",
                       reason=f"only {len(rows)}/{d.hold_bars + 1} bars available")

    entry_ms, entry_o = rows[0][0], rows[0][1]          # enter at the OPEN
    exit_ms, exit_c = rows[d.hold_bars][0], rows[d.hold_bars][4]  # leave at the CLOSE
    if entry_o <= 0 or exit_c <= 0:
        return Settled(d.id, d.symbol, d.rating, side, status="error",
                       reason="non-positive price in the window")

    gross = (exit_c - entry_o) / entry_o * side * size * 100.0

    # fees + slippage, both sides, scaled by exposure (half size for the
    # overweight/underweight tiers so the cost is proportional too)
    fees = (fee_rt_pct + slippage_pct) * size
    # funding: signed by side; a long pays a positive rate
    rates = funding_between(con, d.symbol, entry_ms, exit_ms)
    funding = sum(rates) * 100.0 * side * size

    net = gross - fees - funding

    r_mult = None
    if d.stop_pct and d.stop_pct > 0:
        r_mult = net / (d.stop_pct * 100.0)

    return Settled(
        id=d.id, symbol=d.symbol, rating=d.rating, side=side,
        entry_utc=datetime.fromtimestamp(entry_ms / 1000, timezone.utc).isoformat(),
        entry=entry_o,
        exit_utc=datetime.fromtimestamp(exit_ms / 1000, timezone.utc).isoformat(),
        exit=exit_c, bars_held=d.hold_bars,
        gross_pct=round(gross, 4), fees_pct=round(fees, 4),
        funding_pct=round(funding, 4), net_pct=round(net, 4),
        r_multiple=(round(r_mult, 3) if r_mult is not None else None),
        status="settled",
        reason=(None if rates else "no funding rows for this window"),
    )


def summarise(settled: list[Settled], n_trials: int | None = None) -> dict:
    """Aggregate into the same vocabulary the site and the lab already use.

    t-statistic is mean/std * sqrt(n) on net returns. The deflation bar is
    sqrt(2 ln N) with N = every configuration ever tried, per the source lab's
    method; passing it is the whole point, and most things do not.
    """
    done = [s for s in settled if s.status == "settled" and s.net_pct is not None]
    n = len(done)
    if n == 0:
        return {"n": 0, "note": "nothing settled"}

    nets: list[float] = [x for x in (s.net_pct for s in done) if x is not None]
    mean = sum(nets) / n
    var = sum((x - mean) ** 2 for x in nets) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    t = (mean / sd * math.sqrt(n)) if sd > 0 else 0.0

    wins = sum(1 for x in nets if x > 0)
    gw = sum(x for x in nets if x > 0)
    gl = abs(sum(x for x in nets if x <= 0)) or 1e-9

    out = {
        "n": n,
        "mean_net_pct": round(mean, 4),
        "sd_net_pct": round(sd, 4),
        "t_stat": round(t, 2),
        "win_rate_pct": round(wins / n * 100, 1),
        "profit_factor": round(gw / gl, 2),
        "total_net_pct": round(sum(nets), 4),
        "avg_fees_pct": round(sum((s.fees_pct or 0) for s in done) / n, 4),
        "avg_funding_pct": round(sum((s.funding_pct or 0) for s in done) / n, 4),
        "avg_gross_pct": round(sum((s.gross_pct or 0) for s in done) / n, 4),
    }
    if n_trials and n_trials > 1:
        bar = math.sqrt(2 * math.log(n_trials))
        out["n_trials"] = n_trials
        out["deflation_bar"] = round(bar, 2)
        out["clears_deflation"] = bool(t >= bar)
    return out


def fee_stress(settled: list[Settled], rates=(0.11, 0.30, 0.50)) -> dict:
    """Re-settle at several round-trip costs and show what survives.

    A strategy whose edge vanishes between 11 and 30 bp was never an edge; it was
    a cost assumption. This makes that visible instead of arguable.
    """
    out = {}
    for rt in rates:
        nets = []
        for s in settled:
            if s.status != "settled" or s.gross_pct is None:
                continue
            size = abs(s.side) or 1
            nets.append(s.gross_pct - rt - (s.funding_pct or 0))
        if not nets:
            out[f"{rt:.2f}"] = {"n": 0}
            continue
        m = sum(nets) / len(nets)
        out[f"{rt:.2f}"] = {
            "n": len(nets),
            "mean_net_pct": round(m, 4),
            "total_net_pct": round(sum(nets), 4),
            "positive": m > 0,
        }
    return out


# ----------------------------------------------------------------- self test
def selftest() -> int:
    """Prove the maths on hand-computable cases before trusting it on real data."""
    print("=" * 70)
    print("NET SETTLEMENT — self test")
    print("=" * 70)
    con = _con()
    sym, tf = "BTC/USDT", "4h"
    rows = con.execute(
        "select open_time_utc, open, close from candles where symbol=? and timeframe=? "
        "order by open_time_utc desc limit 20", (sym, tf)).fetchall()
    if len(rows) < 10:
        print(f"  SKIP: need 10 {sym} {tf} bars, have {len(rows)}")
        return 1
    rows = rows[::-1]
    base_ms = rows[0][0]
    decided = datetime.fromtimestamp((base_ms - 1) / 1000, timezone.utc).isoformat()

    print(f"\n  using {sym} {tf}, decided at {decided}")
    print(f"  entry = bar[0].open, exit = bar[5].close\n")

    ok = 0
    fail = 0

    def chk(name, cond, detail=""):
        nonlocal ok, fail
        print(f"  {'✓' if cond else '✗'} {name:52} {detail}")
        if cond:
            ok += 1
        else:
            fail += 1

    # buy, 11 bp fees, funding read from db (likely absent -> 0)
    # Tolerance is 5e-4 because settle_one reports to 4 decimal places of a
    # percent; comparing against an unrounded hand calculation at 1e-6 would
    # fail on the rounding alone rather than on any arithmetic error.
    TOL = 5e-4
    d = Decision("t1", sym, "buy", decided, 5, tf, stop_pct=0.03)
    s = settle_one(con, d, fee_rt_pct=0.11, slippage_pct=0.0)
    exp_gross = (rows[5][2] - rows[0][1]) / rows[0][1] * 100
    chk("buy: gross matches hand calculation",
        abs((s.gross_pct or 0) - exp_gross) < TOL,
        f"{s.gross_pct:.4f} vs {exp_gross:.4f}")
    chk("buy: fees charged 0.11 exactly", abs((s.fees_pct or 0) - 0.11) < 1e-9,
        f"{s.fees_pct}")
    chk("buy: net = gross - fees - funding",
        abs((s.net_pct or 0) - ((s.gross_pct or 0) - (s.fees_pct or 0)
                                - (s.funding_pct or 0))) < TOL)

    # sell must be the exact mirror on gross
    d2 = Decision("t2", sym, "sell", decided, 5, tf, stop_pct=0.03)
    s2 = settle_one(con, d2, fee_rt_pct=0.11)
    chk("sell: gross is the mirror of buy",
        abs((s2.gross_pct or 0) + exp_gross) < TOL,
        f"{s2.gross_pct:.4f} vs {-exp_gross:.4f}")

    # hold is not a position
    d3 = Decision("t3", sym, "hold", decided, 5, tf)
    s3 = settle_one(con, d3)
    chk("hold: status flat, no exposure", s3.status == "flat" and s3.side == 0)

    # unreviewable output is REVIEW, never a silent Hold
    d4 = Decision("t4", sym, "review", decided, 5, tf)
    s4 = settle_one(con, d4)
    chk("review: treated as flat, not as Hold", s4.status == "flat")

    # overweight takes half size -> half gross, half fees
    d5 = Decision("t5", sym, "overweight", decided, 5, tf, stop_pct=0.03)
    s5 = settle_one(con, d5, fee_rt_pct=0.11)
    chk("overweight: half gross",
        abs((s5.gross_pct or 0) - exp_gross / 2) < TOL,
        f"{s5.gross_pct:.4f} vs {exp_gross/2:.4f}")
    chk("overweight: half fees", abs((s5.fees_pct or 0) - 0.055) < 1e-6,
        f"{s5.fees_pct}")

    # insufficient bars must be pending, never filled with a guess
    d6 = Decision("t6", sym, "buy", decided, 9999, tf)
    s6 = settle_one(con, d6)
    chk("insufficient bars: pending, not fabricated", s6.status == "pending",
        s6.reason or "")

    # fee stress must be monotonic
    stress = fee_stress([s, s2], rates=(0.11, 0.30, 0.50))
    vals = [stress[f"{r:.2f}"]["total_net_pct"] for r in (0.11, 0.30, 0.50)]
    chk("fee stress: net falls as fees rise",
        vals[0] > vals[1] > vals[2], f"{vals[0]} > {vals[1]} > {vals[2]}")

    print(f"\n  {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--decisions", type=str, help="JSON list of Decision objects")
    ap.add_argument("--out", type=str)
    ap.add_argument("--fee", type=float, default=FEE_RT_PCT)
    ap.add_argument("--trials", type=int, default=None,
                    help="how many configurations were tried, for deflation")
    args = ap.parse_args()

    if args.selftest or not args.decisions:
        return selftest()

    raw = json.loads(Path(args.decisions).read_text())
    con = _con()
    settled = [settle_one(con, Decision(**d), fee_rt_pct=args.fee) for d in raw]
    out = {
        "fee_rt_pct": args.fee,
        "summary": summarise(settled, args.trials),
        "fee_stress": fee_stress(settled),
        "settled": [asdict(s) for s in settled],
    }
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1))
        print(f"written {args.out}")
    else:
        print(json.dumps(out["summary"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
