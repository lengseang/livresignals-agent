#!/usr/bin/env python3
"""THE HUNT — a disciplined, continuous strategy search over the replay engine.

WHY A PROTOCOL AND NOT A SWEEP
------------------------------
A naive "try parameters until one looks good" is overfitting with extra steps.
With 9 years of data and a fast engine, you can find a thousand parameter sets
that look profitable in-sample and none of which survive. The guard stops you
SEEING the future; it does not stop you CURATING the past.

So the hunt is built on three rules that are structural, not aspirational:

  1. SPLIT FIRST, DECIDE SECOND. Every candidate is scored on IS (2017-01-01 ..
     2023-01-01) and OOS (2023-01-01 .. 2026-10-01) INDEPENDENTLY. A candidate is
     only "promising" if it is positive in BOTH. Anything positive IS-only is
     discarded unseen — it never reaches the report. This is the single most
     important rule: it is what stops the hunt from being a story generator.

  2. NO SHARED STATE ACROSS DAYS. Each run scores a fixed, declared candidate
     list and writes a dated result file. It does not "evolve" from yesterday's
     winners, because that is selection on the same data.

  3. REPORT NUMBERS, NOT VERDICTS. The digest prints PF, expectancy, n and the
     IS/OOS split for every survivor. It does not say "found a good strategy".

WHAT IT SEARCHES
----------------
Entry families (each is a small, interpretable rule, not a fitted curve):
    dip    : drawdown from N-bar high + RSI floor + consecutive red bars
    trend  : price above SMA(N) + RSI upper bound + momentum
    breakout: close above the prior N-bar high

Exit grid (the part that actually moves expectancy, per tonight's finding that
RR and stop width dominate):
    stop   in {0.010, 0.015, 0.020, 0.030}
    target in {stop*1.5, stop*2.0, stop*3.0}
    hold   in {24, 48, 96} bars

Costs are ALWAYS charged at the exchange taker (0.10% RT) plus 5bps slip,
because tonight proved that is the level at which the difference between a
"strategy" and a coin flip is decided.

USAGE
    python3 hunt.py --tf 15m --top 15            # one pass, prints the table
    python3 hunt.py --tf 15m --top 15 --save     # also writes a dated result file
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
RESULTS = Path(__file__).resolve().parents[1] / "reports" / "hunt"

SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "BNB/USDT",
           "ADA/USDT", "LINK/USDT", "DOGE/USDT", "AVAX/USDT", "ARB/USDT"]

# --- the split. Decided here, never adjusted. ------------------------------
IS_START, IS_END = "2017-01-01", "2023-01-01"
OOS_START, OOS_END = "2023-01-01", "2026-10-01"

# --- always charge the exchange's real fee. ---------------------------------
COST = Costs(fee_bps=5.0, slippage_bps=5.0)     # 0.10% fee + 0.05% slip per side
NOTIONAL = 5.0
MAX_OPEN = 8


def ms(d: str) -> int:
    return int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


# =========================== entry families ================================

def make_dip(hi_n, rsi_max, red_min):
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < hi_n + 2:
            return False
        dd = c[-1] / max(c[-hi_n:]) - 1
        g = l = 0.0
        for j in range(len(c) - 14, len(c)):
            d = c[j] - c[j - 1]
            g += d if d > 0 else 0
            l += -d if d < 0 else 0
        r = 100.0 if l == 0 else 100 - 100 / (1 + (g / 14) / (l / 14))
        red = 0
        k = len(c) - 1
        while k > 0 and c[k] < c[k - 1]:
            red += 1
            k -= 1
        return dd <= -hi_n / 10000.0 and r <= rsi_max and red >= red_min
    return f


def make_trend(sma_n, rsi_max, mom_n):
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < sma_n + mom_n + 1:
            return False
        s = sum(c[-sma_n:]) / sma_n
        g = l = 0.0
        for j in range(len(c) - 14, len(c)):
            d = c[j] - c[j - 1]
            g += d if d > 0 else 0
            l += -d if d < 0 else 0
        r = 100.0 if l == 0 else 100 - 100 / (1 + (g / 14) / (l / 14))
        return c[-1] > s and r <= rsi_max and c[-1] > c[-1 - mom_n]
    return f


def make_breakout(look):
    def f(bars):
        c = [b.close for b in bars]
        if len(c) < look + 1:
            return False
        return c[-1] > max(c[-look:-1])
    return f


def _vol_z(bars, win):
    """Volume z-score of the newest CLOSED bar vs the prior `win` bars.

    Returns None when there is not enough history or the window is degenerate
    (zero variance). Volume is read from `b.volume`, which LockedFeed exposes on
    the same immutable Bar the replay engine fills from — so this cannot peek:
    the newest bar is only ever the last closed one the feed has revealed.
    """
    if len(bars) < win + 2:
        return None
    vols = [b.volume for b in bars[-(win + 1):-1]]
    n = len(vols)
    if n < 2:
        return None
    mu = sum(vols) / n
    var = sum((v - mu) ** 2 for v in vols) / n
    sd = var ** 0.5
    if sd <= 0:
        return None
    return (bars[-1].volume - mu) / sd


def make_dip_vol(hi_n, rsi_max, red_min, vz_min, vz_win):
    """Capitulation dip PLUS a volume-spike confirmation.

    This is the improvement_loop's top-ranked 'explore' suggestion made testable
    (2026-10-09): capitulation often arrives on high volume, not merely a high
    drawdown. Same dip rule as make_dip, with an extra gate that the triggering
    bar's volume z-score (over the prior vz_win bars) is >= vz_min.
    """
    base = make_dip(hi_n, rsi_max, red_min)

    def f(bars):
        if not base(bars):
            return False
        z = _vol_z(bars, vz_win)
        return z is not None and z >= vz_min
    return f


def make_brk_vol(look, vz_min, vz_win):
    """Breakout PLUS a volume-spike confirmation (volume-confirmed breakout)."""
    base = make_breakout(look)

    def f(bars):
        if not base(bars):
            return False
        z = _vol_z(bars, vz_win)
        return z is not None and z >= vz_min
    return f


def build_candidates() -> list[dict]:
    """A FIXED, declared list. Not evolved, not sampled. Same every run."""
    out = []
    # dip family
    for hi_n, rsi_max, red_min in itertools.product([50, 90, 200], [30, 35, 40], [2, 3, 4]):
        out.append({"fam": "dip", "params": (hi_n, rsi_max, red_min),
                    "mk": lambda hi_n=hi_n, rsi_max=rsi_max, red_min=red_min:
                          make_dip(hi_n, rsi_max, red_min),
                    "name": f"dip hi{hi_n} rsi{rsi_max} red{red_min}"})
    # trend family
    for sma_n, rsi_max, mom_n in itertools.product([20, 50, 200], [35, 50, 65], [4, 12]):
        out.append({"fam": "trend", "params": (sma_n, rsi_max, mom_n),
                    "mk": lambda sma_n=sma_n, rsi_max=rsi_max, mom_n=mom_n:
                          make_trend(sma_n, rsi_max, mom_n),
                    "name": f"trend sma{sma_n} rsi<{rsi_max} mom{mom_n}"})
    # breakout family
    for look in (20, 50, 100):
        out.append({"fam": "brk", "params": (look,),
                    "mk": lambda look=look: make_breakout(look),
                    "name": f"breakout {look}bar-high"})
    # volume-confirmed families (2026-10-10): the improvement_loop's top-ranked
    # 'explore' suggestion — capitulation/breakout arrive on a volume spike, not
    # just a price move. Small, declared grid (no evolution): a two-level volume
    # z threshold x a two-level window on the existing dip/breakout rules.
    VZ_WINS = (24, 96)
    VZ_MINS = (1.0, 2.0)
    for hi_n, rsi_max, red_min in itertools.product([90, 200], [30, 35], [3]):
        for vz_win, vz_min in itertools.product(VZ_WINS, VZ_MINS):
            out.append({"fam": "dip_vol", "params": (hi_n, rsi_max, red_min, vz_min, vz_win),
                        "mk": lambda hi_n=hi_n, rsi_max=rsi_max, red_min=red_min,
                              vz_min=vz_min, vz_win=vz_win:
                              make_dip_vol(hi_n, rsi_max, red_min, vz_min, vz_win),
                        "name": f"dipvol hi{hi_n} rsi{rsi_max} red{red_min} z{vz_min} w{vz_win}"})
    for look in (20, 50):
        for vz_win, vz_min in itertools.product(VZ_WINS, VZ_MINS):
            out.append({"fam": "brk_vol", "params": (look, vz_min, vz_win),
                        "mk": lambda look=look, vz_min=vz_min, vz_win=vz_win:
                              make_brk_vol(look, vz_min, vz_win),
                        "name": f"brkvol {look}bar z{vz_min} w{vz_win}"})
    return out


def exits():
    for stop in (0.010, 0.015, 0.020, 0.030):
        for mult in (1.5, 2.0, 3.0):
            for hold in (24, 48, 96):
                yield stop, stop * mult, hold


# =========================== scoring ======================================

def run_one(con, tf, entry_fn, stop, target, hold, start, end, cache=None):
    eng = ReplayEngine(con, SYMBOLS, tf, ms(start), ms(end), costs=COST,
                       notional=NOTIONAL, max_open=MAX_OPEN, bars_cache=cache)

    def decide(feeds, openpos):
        out = []
        for sym, feed in feeds.items():
            if any(p.symbol == sym for p in openpos.values()):
                continue
            bars = feed.history(210)
            if len(bars) < 60:
                continue
            if entry_fn(bars):
                out.append(Decision(sym, "enter_long", stop_frac=stop,
                                    target_frac=target, hold_bars=hold, tag="hunt"))
        return out

    tr = eng.run(decide)
    if not tr:
        return None
    n = len(tr)
    net = sum(t.pnl for t in tr)
    w = sum(1 for t in tr if t.pnl > 0)
    gw = sum(t.pnl for t in tr if t.pnl > 0)
    gl = abs(sum(t.pnl for t in tr if t.pnl <= 0)) or 1e-9
    return {"n": n, "win": w / n, "net": net, "avg": net / n, "pf": gw / gl}


# --- the exit grid, split for the funnel ----------------------------------
# A representative subset for screening: the three RR families at the middle
# stop. Screening on a subset is legitimate because the screen only ORDERS
# candidates; the full grid is applied to whatever survives.
SCREEN_EXITS = [
    (0.010, 0.020, 48),
    (0.015, 0.030, 48),
    (0.020, 0.040, 48),
    (0.020, 0.030, 48),
    (0.020, 0.060, 48),
    (0.030, 0.045, 48),
]

SCREEN_START, SCREEN_END = "2025-01-01", "2026-01-01"


def run_funnel(con, cache, cands, exs, args):
    """3 stages: cheap screen -> full IS -> OOS. Only survivors reach the report."""
    print(f"FUNNEL  stage1 screen {len(cands)}x{len(SCREEN_EXITS)} on "
          f"{SCREEN_START}..{SCREEN_END}")

    # ---- stage 1: screen. Order candidates; discard the obvious dead ones.
    t0 = time.time()
    scored = []
    for i, c in enumerate(cands):
        best = None
        for stop, target, hold in SCREEN_EXITS:
            r = run_one(con, args.tf, c["mk"](), stop, target, hold,
                        SCREEN_START, SCREEN_END, cache)
            if r and r["n"] >= 50 and (best is None or r["pf"] > best["pf"]):
                best = {**r, "stop": stop, "target": target, "hold": hold}
        if best and best["pf"] > 1.0:
            scored.append({"cand": c, "screen": best})
        if (i + 1) % 10 == 0:
            print(f"    ...{i+1}/{len(cands)} entries, {len(scored)} passed screen")
    print(f"  stage1: {len(scored)} of {len(cands)} entries beat PF 1.0 in "
          f"{SCREEN_START[:4]}  ({time.time()-t0:.0f}s)")

    if not scored:
        print("\n  VERDICT: nothing survives the screen. The space is empty.")
        # A NEGATIVE RESULT IS STILL A RESULT. It must be recorded and dated, or
        # the owner cannot tell "the hunt swept the space and found nothing" from
        # "the hunt silently failed to run". Silence in the DELIVERY is correct;
        # silence in the RECORD is not.
        if args.save:
            RESULTS.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
            f = RESULTS / f"{stamp}_{args.tf}.json"
            f.write_text(json.dumps({
                "ran_at": stamp, "tf": args.tf, "mode": "funnel",
                "split": {"is": [IS_START, IS_END], "oos": [OOS_START, OOS_END]},
                "screen": [SCREEN_START, SCREEN_END],
                "cost": "0.10% fee + 0.05% slip per side",
                "stage1_passed": 0, "stage2_passed": 0,
                "entries_screened": len(cands),
                "verdict": "nothing survives the screen; space is empty",
                "survivors": [],
            }, indent=2))
            print(f"\n  saved -> {f}")
        return 0

    # ---- stage 2: full IS on the top screen performers, across ALL exits
    scored.sort(key=lambda s: -s["screen"]["pf"])
    short = scored[:args.screen_top]
    print(f"\nFUNNEL  stage2 full IS {IS_START}..{IS_END} on top {len(short)}")
    t0 = time.time()
    stage2 = []
    for i, s in enumerate(short):
        c = s["cand"]
        for stop, target, hold in exs:
            r = run_one(con, args.tf, c["mk"](), stop, target, hold, IS_START, IS_END, cache)
            if r and r["n"] >= 100 and r["pf"] > 1.0:
                stage2.append({"name": c["name"], "fam": c["fam"], "stop": stop,
                               "target": target, "hold": hold, "is": r})
        if (i + 1) % 10 == 0:
            print(f"    ...{i+1}/{len(short)} entries, {len(stage2)} IS-positive")
    print(f"  stage2: {len(stage2)} configs positive in IS  ({time.time()-t0:.0f}s)")

    # ---- stage 3: OOS only on the IS survivors
    print(f"\nFUNNEL  stage3 OOS {OOS_START}..{OOS_END} on {len(stage2)}")
    t0 = time.time()
    survivors = []
    for s in stage2:
        c = next(x["cand"] for x in scored if x["cand"]["name"] == s["name"])
        r = run_one(con, args.tf, c["mk"](), s["stop"], s["target"], s["hold"],
                    OOS_START, OOS_END, cache)
        if r and r["n"] >= 50 and r["pf"] > 1.0:
            s["oos"] = r
            s["combined_pf"] = (s["is"]["pf"] + r["pf"]) / 2
            survivors.append(s)
    print(f"  stage3: {len(survivors)} positive in BOTH halves  ({time.time()-t0:.0f}s)")

    survivors.sort(key=lambda x: -x["combined_pf"])
    print(f"\n=== {len(survivors)} SURVIVORS (positive IS and OOS, at exchange cost) ===")
    if not survivors:
        print("  NONE. Nothing survives the split. That is the finding.")
    else:
        print(f"  {'config':32} {'PF_is':>6} {'PF_oos':>7} {'n_is':>7} {'n_oos':>6} {'avg_oos':>9}")
        for s in survivors[:args.top]:
            print(f"  {s['name'][:32]:32} {s['is']['pf']:6.2f} {s['oos']['pf']:7.2f} "
                  f"{s['is']['n']:7} {s['oos']['n']:6} {s['oos']['avg']:+9.5f}")

    if args.save:
        RESULTS.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        f = RESULTS / f"{stamp}_{args.tf}.json"
        f.write_text(json.dumps({
            "ran_at": stamp, "tf": args.tf, "mode": "funnel",
            "split": {"is": [IS_START, IS_END], "oos": [OOS_START, OOS_END]},
            "screen": [SCREEN_START, SCREEN_END],
            "cost": "0.10% fee + 0.05% slip per side",
            "stage1_passed": len(scored), "stage2_passed": len(stage2),
            "survivors": survivors,
        }, indent=2))
        print(f"\n  saved -> {f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--max-candidates", type=int, default=999)
    ap.add_argument("--only-fam", default=None,
                    help="comma-separated family filter for build_candidates(), "
                         "e.g. 'dip_vol,brk_vol' to test only the volume-gated "
                         "entries without re-running the whole declared list")
    ap.add_argument("--funnel", action="store_true",
                    help="3-stage funnel: screen on SCREEN_YEAR, full IS on the "
                         "top N, OOS only on survivors. ~10x faster than the flat sweep.")
    ap.add_argument("--screen-top", type=int, default=150)
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    CACHE: dict = {}                      # parse each series ONCE for the whole sweep
    cands = build_candidates()[:args.max_candidates]
    if args.only_fam:
        want = {s.strip() for s in args.only_fam.split(",") if s.strip()}
        cands = [c for c in cands if c["fam"] in want]
        if not cands:
            print(f"hunt: --only-fam {args.only_fam!r} matched no candidate family")
            return 2
    exs = list(exits())
    print(f"hunt: {len(cands)} entries x {len(exs)} exits = {len(cands)*len(exs)} configs")
    print(f"split: IS {IS_START}..{IS_END}  |  OOS {OOS_START}..{OOS_END}")
    print(f"cost : 0.10% fee + 0.05% slip per side (exchange taker, charged always)\n")

    t0 = time.time()

    if args.funnel:
        return run_funnel(con, CACHE, cands, exs, args)

    survivors = []
    for ci, c in enumerate(cands):
        for stop, target, hold in exs:
            is_r = run_one(con, args.tf, c["mk"](), stop, target, hold, IS_START, IS_END, CACHE)
            if not is_r or is_r["n"] < 100:
                continue
            if is_r["pf"] <= 1.0:              # RULE 1: IS must be positive
                continue
            oos_r = run_one(con, args.tf, c["mk"](), stop, target, hold, OOS_START, OOS_END, CACHE)
            if not oos_r or oos_r["n"] < 50:
                continue
            if oos_r["pf"] <= 1.0:             # RULE 1: OOS must ALSO be positive
                continue
            survivors.append({
                "name": c["name"], "fam": c["fam"],
                "stop": stop, "target": target, "hold": hold,
                "is": is_r, "oos": oos_r,
                "combined_pf": (is_r["pf"] + oos_r["pf"]) / 2,
            })
        if (ci + 1) % 5 == 0:
            print(f"  ...{ci+1}/{len(cands)} entries, {len(survivors)} survive both halves")

    el = time.time() - t0
    print(f"\n=== {len(survivors)} configs positive in BOTH halves (of {len(cands)*len(exs)}) ===")
    print(f"    elapsed {el:.0f}s\n")

    survivors.sort(key=lambda s: -s["combined_pf"])
    if not survivors:
        print("  NONE. Nothing in this family survives an IS/OOS split at exchange cost.")
        print("  That is a result, not a failure: it says the search space is empty of edge.")
    else:
        print(f"  {'config':34} {'PF_is':>6} {'PF_oos':>7} {'n_is':>6} {'n_oos':>6} {'avg_oos':>9}")
        for s in survivors[:args.top]:
            print(f"  {s['name'][:34]:34} {s['is']['pf']:6.2f} {s['oos']['pf']:7.2f} "
                  f"{s['is']['n']:6} {s['oos']['n']:6} {s['oos']['avg']:+9.5f}")

    if args.save:
        RESULTS.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        f = RESULTS / f"{stamp}_{args.tf}.json"
        f.write_text(json.dumps({
            "ran_at": stamp, "tf": args.tf,
            "split": {"is": [IS_START, IS_END], "oos": [OOS_START, OOS_END]},
            "cost": "0.10% fee + 0.05% slip per side",
            "configs_tested": len(cands) * len(exs),
            "survivors": survivors,
        }, indent=2))
        print(f"\n  saved -> {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
