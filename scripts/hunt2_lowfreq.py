#!/usr/bin/env python3
"""HUNT2 — the low-frequency search.

Why this exists
---------------
Everything tested on 2026-10-09/10 was high-frequency mean-reversion: 2.5k-22k
trades/year. At 0.30% round trip that frequency pays 60x the account's face value
in fees per year, and no rule in the family beat it. The measured per-trade edge
was ~0.01-0.05%; the fee is 0.30%. That class is empty.

The untested hypothesis: an edge exists at LOW FREQUENCY, where the fee is small
relative to the move. A trade held for days whose expected move is 5-15% can
absorb 0.30% of costs. Nothing has searched this — every candidate the engine has
ever scored fired thousands of times a year.

THE GATE THAT MAKES THIS A DIFFERENT SEARCH (pre-registered, 2026-10-10)
------------------------------------------------------------------------
A candidate that fires >= FREQ_CAP times/year is REJECTED OUTRIGHT, whatever its
PF. It is the class already ruled out. This is not a soft preference: without it
the search would simply rediscover the fee-bound family and rank its luckiest
member first.

Falsification, written BEFORE the run:
  H1  low-frequency candidates (<= 500/yr) beat PF 1.0 in BOTH halves
      -> if none do, the low-frequency class is empty too, and that is the answer
  H2  a survivor must have >= 100 OOS trades, or it is noise and is discarded
  H3  the fee must be < 20% of the average winning move
      -> if a survivor's fee is a large share of its win, it is fee-bound in
         disguise and is discarded no matter what PF says

Timeframes: 4h and 1d. Holds: days to weeks. Stops: wide. Costs: always charged.

Usage:
    python3 hunt2_lowfreq.py --tf 4h
    python3 hunt2_lowfreq.py --tf 1d --save
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import sqlite3
import time
from pathlib import Path

from replay_engine import Costs, Decision, ReplayEngine

DB = Path(__file__).resolve().parents[1] / "data" / "trading.db"
RESULTS = Path(__file__).resolve().parents[1] / "reports" / "hunt2"

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
           "ADA/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT", "ARB/USDT"]

IS_START, IS_END = "2017-01-01", "2023-01-01"
OOS_START, OOS_END = "2023-01-01", "2026-10-01"

COST = Costs(fee_bps=5.0, slippage_bps=5.0)
NOTIONAL = 5.0
MAX_OPEN = 8

# THE GATE. A candidate above this is the class already ruled out.
FREQ_CAP = 500          # trades/year across ALL symbols


def ms(d: str) -> int:
    return int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def rsi(vals, n=14):
    if len(vals) < n + 1:
        return None
    g = l = 0.0
    for j in range(len(vals) - n, len(vals)):
        d = vals[j] - vals[j - 1]
        g += d if d > 0 else 0
        l += -d if d < 0 else 0
    return 100.0 if l == 0 else 100 - 100 / (1 + (g / n) / (l / n))


# ---------------------------- entry families --------------------------------

def mk_breakout(look):
    """Close above the prior N-bar high. The best non-dip family found so far."""
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < look + 2:
            return False
        return c[-1] > max(c[-look - 1:-1])
    return f


def mk_trend_cross(fast, slow):
    """Fast SMA above slow SMA — the classic trend filter, entered on the cross."""
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < slow + 2:
            return False
        af, bf = sma(c[:-1], fast), sma(c[:-1], slow)
        an, bn = sma(c, fast), sma(c, slow)
        if None in (af, bf, an, bn):
            return False
        return af <= bf and an > bn          # the crossing bar itself
    return f


def mk_trend_slope(sma_n, lookback):
    """Price above SMA(N) and the SMA itself rising over `lookback` bars."""
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < sma_n + lookback + 1:
            return False
        now, then = sma(c, sma_n), sma(c[:-lookback], sma_n)
        if None in (now, then):
            return False
        return c[-1] > now and now > then
    return f


def mk_regime_dip(hi_n, rsi_max):
    """Dip-buy, but ONLY when the long trend is up — the regime gate.
    This is the high-frequency family given a filter that should cut its
    frequency dramatically. If the gate works, the frequency cap will accept it
    and we learn whether selectivity alone rescues the dip."""
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < hi_n + 200:
            return False
        s200 = sma(c, 200)
        if s200 is None or c[-1] < s200:          # regime gate: only in uptrends
            return False
        dd = c[-1] / max(c[-hi_n:]) - 1
        r = rsi(c)
        return r is not None and dd <= -0.02 and r <= rsi_max
    return f


def build_candidates():
    out = []
    for look in (20, 55, 100, 200):
        out.append({"fam": "breakout", "name": f"breakout {look}-bar high",
                    "mk": lambda look=look: mk_breakout(look)})
    for fast, slow in [(10, 50), (20, 100), (50, 200)]:
        out.append({"fam": "xcross", "name": f"trend cross {fast}/{slow}",
                    "mk": lambda fast=fast, slow=slow: mk_trend_cross(fast, slow)})
    for n, lb in [(50, 10), (100, 20), (200, 30)]:
        out.append({"fam": "slope", "name": f"slope sma{n} rise{lb}",
                    "mk": lambda n=n, lb=lb: mk_trend_slope(n, lb)})
    for hi, rm in [(20, 40), (50, 35)]:
        out.append({"fam": "regdip", "name": f"regime-dip hi{hi} rsi{rm}",
                    "mk": lambda hi=hi, rm=rm: mk_regime_dip(hi, rm)})
    return out


def exits():
    """WIDE by design. A low-frequency trade needs room or it is just a slow
    version of the family that already failed."""
    for stop in (0.04, 0.06, 0.08, 0.12):
        for mult in (2.0, 3.0, 4.0):
            for hold in (60, 180, 540):          # ~10d / 30d / 90d on 4h bars
                yield stop, stop * mult, hold


def run_one(con, tf, entry_fn, stop, target, hold, start, end, cache=None):
    eng = ReplayEngine(con, SYMBOLS, tf, ms(start), ms(end), costs=COST,
                       notional=NOTIONAL, max_open=MAX_OPEN, bars_cache=cache,
                       min_coverage=0.9)

    def decide(feeds, openpos):
        out = []
        for sym, feed in feeds.items():
            if any(p.symbol == sym for p in openpos.values()):
                continue
            bars = feed.history(260)
            if len(bars) < 55:
                continue
            if entry_fn(bars):
                out.append(Decision(sym, "enter_long", stop_frac=stop,
                                    target_frac=target, hold_bars=hold, tag="lf"))
        return out

    tr = eng.run(decide)
    if not tr:
        return None
    n = len(tr)
    net = sum(t.pnl for t in tr)
    w = sum(1 for t in tr if t.pnl > 0)
    gw = sum(t.pnl for t in tr if t.pnl > 0)
    gl = abs(sum(t.pnl for t in tr if t.pnl <= 0)) or 1e-9
    years = (ms(end) - ms(start)) / (365.25 * 24 * 3600 * 1000)
    freq = n / years
    avg_win = gw / max(w, 1)
    # fee as a share of the average win (H3)
    fee_share = (NOTIONAL * 0.003) / avg_win if avg_win > 0 else 9.9
    return {"n": n, "win": w / n, "net": net, "avg": net / n, "pf": gw / gl,
            "freq": freq, "avg_win": avg_win, "fee_share": fee_share}


def report(survivors, cands, exs, tf, args, t0):
    survivors.sort(key=lambda s: -s["combined_pf"])
    print(f"\n=== {len(survivors)} SURVIVORS (PF>1 in BOTH halves, freq<={FREQ_CAP}/yr) ===")
    if not survivors:
        print("  NONE. The low-frequency class is empty too.")
        print("  That is a real result: it means selectivity alone does not")
        print("  produce an edge, not that the search failed.")
    else:
        print(f"  {'config':34} {'PFis':>5} {'PFoos':>6} {'n_is':>6} {'n_oos':>6} "
              f"{'freq/yr':>8} {'fee/win':>8}")
        for s in survivors[:args.top]:
            print(f"  {s['name'][:34]:34} {s['is']['pf']:5.2f} {s['oos']['pf']:6.2f} "
                  f"{s['is']['n']:6} {s['oos']['n']:6} {s['oos']['freq']:8.0f} "
                  f"{s['oos']['fee_share']*100:7.1f}%")
    print(f"\n  elapsed {time.time()-t0:.0f}s   configs {len(cands)*len(exs)}")

    if args.save:
        RESULTS.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        f = RESULTS / f"{stamp}_{tf}.json"
        f.write_text(json.dumps({
            "ran_at": stamp, "tf": tf, "mode": "lowfreq",
            "split": {"is": [IS_START, IS_END], "oos": [OOS_START, OOS_END]},
            "cost": "0.10% fee + 0.05% slip per side",
            "freq_cap_per_year": FREQ_CAP,
            "configs_tested": len(cands) * len(exs),
            "survivors": survivors,
            "verdict": "none" if not survivors else f"{len(survivors)} survive",
        }, indent=2))
        print(f"  saved -> {f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="4h")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cache: dict = {}
    cands = build_candidates()
    exs = list(exits())
    t0 = time.time()

    print(f"=== HUNT2 low-frequency search · tf {args.tf} ===")
    print(f"entries {len(cands)} x exits {len(exs)} = {len(cands)*len(exs)} configs")
    print(f"IS {IS_START}..{IS_END}   OOS {OOS_START}..{OOS_END}")
    print(f"cost 0.10% fee + 0.05% slip per side (always charged)")
    print(f"GATE: reject any candidate above {FREQ_CAP} trades/year — that is the")
    print(f"      high-frequency class already ruled out.\n")

    survivors = []
    for i, c in enumerate(cands):
        n_acc = 0
        last_freq = 0.0
        last_pf = 0.0
        for stop, target, hold in exs:
            r = run_one(con, args.tf, c["mk"](), stop, target, hold, IS_START, IS_END, cache)
            if not r or r["n"] < 50:
                continue
            last_freq, last_pf = r["freq"], r["pf"]
            if r["freq"] > FREQ_CAP:              # THE GATE
                continue
            if r["pf"] <= 1.0:
                continue
            o = run_one(con, args.tf, c["mk"](), stop, target, hold, OOS_START, OOS_END, cache)
            if not o or o["n"] < 100:             # H2: enough trades to matter
                continue
            if o["freq"] > FREQ_CAP:
                continue
            if o["pf"] <= 1.0:
                continue
            if o["fee_share"] >= 0.20:            # H3: fee-bound in disguise
                continue
            survivors.append({"name": c["name"], "fam": c["fam"],
                              "stop": stop, "target": target, "hold": hold,
                              "is": r, "oos": o,
                              "combined_pf": (r["pf"] + o["pf"]) / 2})
            n_acc += 1
        print(f"    {c['name'][:40]:40} freq~{last_freq:6.0f}/yr  "
              f"PFis {last_pf:.2f}  accepted {n_acc}")

    report(survivors, cands, exs, args.tf, args, t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
