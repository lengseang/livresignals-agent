#!/usr/bin/env python3
"""Unified REAL + PAPER trader — owner's mandate.

Rules (per owner directive):
  1. Scan for VALIDATED capitulation signals (deep dip + low RSI + red bars).
  2. Validate each candidate against the walk-forward edge BEFORE edging.
  3. If real balance allows a $5 position  -> fire REAL money (live_executor).
  4. If real balance too low               -> fire PAPER only.
  5. ALWAYS run PAPER in parallel (learning + paper P&L), regardless.
  6. Never exceed the conservative live caps (1% stop, $5, $1/day, 1 open).

This is the single entry point that reconciles the two accounts. Run via cron
every 30m (alongside the engine). The engine does paper; this adds the live leg
+ the always-on parallel paper track.

Validate-before-edge: a signal only qualifies if its (dip,rsi,red) bucket has a
positive expectancy in the walk-forward evidence. We hardcode the validated
buckets from walkforward_full.py:
  XRP 30m dip<=-8% rsi<=30 3-4red -> WR 72% EV +0.96%
  ETH 15m dip<=-6% rsi<=40 4red   -> WR 72% EV +1.14%
  BTC 5m  dip<=-0.5% rsi<=30 3red -> WR 66% EV +0.83%
Plus the general rule: shallow-but-deep-enough dips with rsi<=30 and >=3 red.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
PAPER_STATE = ROOT / "data" / "unified-paper.json"

# RELIABILITY FIX (2026-10-01): set False when ex.load_markets() fails after
# retries. A degraded cycle still runs paper + position management + futures,
# but _real_can_trade() returns False so no real order is attempted without a
# healthy market/precision cache. Assumed True until main() proves otherwise,
# so any non-main caller (tests, imports) keeps the normal real path.
_MARKETS_OK = True

# --- validated edges (from walkforward_full.py, must hold to fire) ---
VALIDATED = {
    ("XRP/USDT", "30m"): {"dip": -0.08, "rsi": 30, "red": 3, "wr": 0.724, "ev": 0.0096},
    ("ETH/USDT", "15m"): {"dip": -0.06, "rsi": 40, "red": 4, "wr": 0.72, "ev": 0.0114},
    ("BTC/USDT", "5m"):  {"dip": -0.005, "rsi": 30, "red": 3, "wr": 0.66, "ev": 0.0083},
    # shallow-dip alts — owner-approved second edge (validated 72% WR, fires more often)
    ("XRP/USDT", "15m"): {"dip": -0.05, "rsi": 40, "red": 3, "wr": 0.70, "ev": 0.0080},
    ("ETH/USDT", "30m"): {"dip": -0.06, "rsi": 40, "red": 3, "wr": 0.70, "ev": 0.0090},
}

SYMS = ["BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT", "DOGE/USDT",
        "ADA/USDT", "LINK/USDT", "AVAX/USDT", "BNB/USDT", "SUI/USDT"]
# KNOWLEDGE-EXPANSION WATCHLIST (2026-09-06): hot movers NOT yet tradeable for
# real (lot-rounding / not in ALLOWED_REAL) but scanned for PAPER learning so we
# study their behavior and are ready when the account grows. ARB +39%, JUP +20%,
# NEAR +10%, OP +9% today — the kind of runner our trend-adaptive scan should
# recognize. Kept small (4) to bound scan time on the 909MB box.
WATCH_SYMS = ["ARB/USDT", "JUP/USDT", "NEAR/USDT", "OP/USDT"]
SYMS = SYMS + WATCH_SYMS
TFS = ["5m", "15m", "30m"]
# SYMBOL TIERS from paper ledger (2026-09-09, 113 native trades): real money ONLY
# on proven symbols. ARB 31% WR -$0.32 = trap (chasing +39% meme lost). Proven:
# NEAR 80%, DOGE 83%, ETH 73%, LINK 56%, SOL 55%, XRP 54%. Watchlist memes
# (ARB/JUP/OP) stay PAPER-ONLY until they prove >=55% WR over 15+ trades.
# ELITE (2026-09-09 backtest, 600-800 candles): 15m SHARP (rsi<=35) measured
# ETH 87-88% · SOL 80-90% · XRP 68-93% — the highest-confidence setups found.
# Real money fires ONLY on these (15m + sharp). Everything else = paper learning.
ELITE_SYMS = {"ETH/USDT", "SOL/USDT", "XRP/USDT"}
REAL_TIER1 = {"NEAR/USDT", "ETH/USDT", "DOGE/USDT", "LINK/USDT", "SOL/USDT", "XRP/USDT"}
REAL_TIER2 = {"BTC/USDT", "ADA/USDT", "BNB/USDT", "AVAX/USDT", "SUI/USDT", "TON/USDT", "TRX/USDT"}
# STRATEGY SHIFT 2026-09-06 (owner: option-4 aggressive): the 152-trade paper
# ledger says 5m scalps (69% WR, +$0.37) and 15m (60% WR, +$0.14) are the real
# earners; 30m is a coin-flip (52%, +$0.01). ETH (+$0.25) and XRP (+$0.04) are
# the winning symbols. Scan order is priority-weighted: CORE_TF (5m/15m) scan
# before 30m so slots fill with proven edges first.
CORE_TFS = ["5m", "15m"]
CORE_SYMS = ["ETH/USDT", "XRP/USDT"]  # proven winners — first claim on slots


def _features(ex, sym: str, tf: str) -> dict | None:
    try:
        ohlcv = ex.fetch_ohlcv(sym, tf, limit=120)
    except Exception:
        return None
    # DROP THE FORMING BAR. Binance's array ends with the in-progress candle, and
    # every feature below (drawdown vs the 90-bar high, RSI, red-streak) is then
    # measured on a partial bar whose close can still move for the rest of the
    # window: mean intrabar range is 0.30% on 15m and 0.67% on 1h, so the RSI and
    # the red-count flip on ordinary moves. These sigs gate REAL orders, so a
    # prediction taken on an unfinished bar is a decision on data that does not
    # exist yet. Same predicate as elsewhere: a bar is closed once now >= open+tf.
    _tf_ms = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000,
              "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.get(tf)
    if _tf_ms:
        _now = int(time.time() * 1000)
        ohlcv = [c for c in ohlcv if int(c[0]) + _tf_ms <= _now]
    closes = [c[4] for c in ohlcv]
    if len(closes) < 90:
        return None
    hi = max(closes[-90:])
    dd = closes[-1] / hi - 1
    n = 14
    g = l = 0.0
    for j in range(-n + 1, 0):
        dx = closes[j] - closes[j - 1]
        if dx > 0:
            g += dx
        else:
            l -= dx
    rsi = 100.0 if l == 0 else 100 - 100 / (1 + (g / n) / (l / n))
    red = 0
    k = len(closes) - 1
    while k > 0 and closes[k] < closes[k - 1]:
        red += 1
        k -= 1
    # trend context: EMA slope over last 40 bars (uptrend if rising)
    ema = 0.0
    mult = 2 / (min(40, len(closes)) + 1)
    for c in closes:
        ema = c * mult + ema * (1 - mult)
    ema_prev = 0.0
    for c in closes[:-4]:
        ema_prev = c * mult + ema_prev * (1 - mult)
    slope = (ema - ema_prev) / (ema_prev or 1)
    # ATR(14) volatility — for ADAPTIVE TP/SL: wide stops in wild markets,
    # tight stops in calm ones (2026-09-06 owner request)
    trs = []
    for j in range(1, min(15, len(ohlcv))):
        h = ohlcv[-j][2]
        l = ohlcv[-j][3]
        pc = ohlcv[-j - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = (sum(trs) / len(trs)) if trs else 0.0
    atr_pct = atr / (closes[-1] or 1) * 100  # ATR as % of price
    # momentum: 12-bar ROC — strong momentum = wider TP room (ride the runner)
    mom = (closes[-1] / closes[-13] - 1) * 100 if len(closes) > 13 else 0.0
    return {"symbol": sym, "tf": tf, "dd": dd, "rsi": rsi, "red": red,
            "close": closes[-1], "trend": slope, "atr_pct": atr_pct,
            "mom": mom}


def _validated(sig: dict) -> dict | None:
    """Return the validated edge if this signal matches a proven bucket, else None."""
    key = (sig["symbol"], sig["tf"])
    v = VALIDATED.get(key)
    if not v:
        # general fallback: deep enough + rsi<=30 + >=3 red (matches engine rule)
        if sig["dd"] <= -0.02 and sig["rsi"] <= 30 and sig["red"] >= 3:
            return {"wr": 0.66, "ev": 0.0083, "fallback": True}
        return None
    if sig["dd"] <= v["dip"] and sig["rsi"] <= v["rsi"] and sig["red"] >= v["red"]:
        return v
    return None


# --- PAPER COST MODEL (2026-10-05) -----------------------------------------
# The paper book booked GROSS pnl: it applied no transaction cost at all, so its
# P&L measured the strategy's gross behaviour and not its profitability. That gap
# was measured, not guessed: the walk-forward replay (docs/proposals/replay-findings.md)
# shows the live rule earns PF 1.02-1.04 at the real fills' implied cost but is
# negative at the exchange's own taker fee. A paper track with no costs cannot
# show that, and reported +0.0095/trade where the replay supports +0.0006..+0.0013.
#
# PAPER_COST_RT is the round-trip fraction charged on close, as a FRACTION (not
# bps). 0.0003 = 0.03% round trip, taken from the 30 real fills on this account
# (recovered fees: +0.027%, -0.001%, +0.059% — median ~zero, mean ~0.03%).
# Deliberately conservative: it is the LOW end, so the paper book is flattered
# rather than punished. Set PAPER_COST_RT=0 to restore gross-only booking.
#
# `pnl` stays the GROSS number so every existing consumer, threshold and
# comparison keeps working unchanged. `pnl_net` is the new, honest figure, and
# `cost` records what was charged. Nothing else about the book changes.
#
# MEASURED-COST FIX (2026-10-11). The 0.0003 above was a HARDCODED constant
# taken from "30 real fills" whose recovered fees were ~zero. It went stale the
# moment the exchange's real fees were actually measured: `fee_audit.py` (cron
# 09:50 daily) now reports `measured_round_trip_pct = 0.16092%` — 5.4x the
# charged 0.03%. So the book was NOT "deliberately conservative"; it was
# OPTIMISTIC by ~0.13%/trade. On 2123 trades that is ~$13.8 of hidden cost —
# enough to flip the reported +$18.98 net to about -$3.35. (Same class of defect
# as the stale edge feed and the missing regime tag: a frozen assumption the
# live measurement has since overtaken.)
#
# Resolution: PREFER the measured round-trip from data/fee-audit.json. An
# explicit PAPER_COST_RT env var still wins (operator override); the stale
# 0.0003 is kept only as the last-resort fallback when no audit file exists.
def _measured_round_trip() -> float | None:
    """The exchange's ACTUAL round-trip cost fraction, from the fee audit.

    Percent in the file (0.16092) -> fraction (0.0016092). None when the audit
    is missing/unreadable, so callers fall back rather than silently taking 0.
    """
    try:
        p = ROOT / "data" / "fee-audit.json"
        if not p.exists():
            return None
        tot = json.loads(p.read_text()).get("totals", {})
        pct = float(tot.get("measured_round_trip_pct") or 0.0)
        return pct / 100.0 if pct > 0 else None
    except Exception:  # noqa: BLE001 — a bad audit file must not crash the trader
        return None


_CONSERVATIVE_FALLBACK_RT = 0.0003
_MEASURED_RT = _measured_round_trip()
if os.environ.get("PAPER_COST_RT") is not None:
    PAPER_COST_RT = float(os.environ["PAPER_COST_RT"])          # explicit override
    _COST_SOURCE = "env PAPER_COST_RT"
elif _MEASURED_RT is not None:
    PAPER_COST_RT = _MEASURED_RT                                 # measured (preferred)
    _COST_SOURCE = "fee-audit.json measured_round_trip_pct"
else:
    PAPER_COST_RT = _CONSERVATIVE_FALLBACK_RT                    # last resort
    _COST_SOURCE = "hardcoded fallback (no fee-audit.json)"

# PAPER_CHARGE_COST=0 disables charging entirely (records pnl_net == pnl).
PAPER_CHARGE_COST = os.environ.get("PAPER_CHARGE_COST", "1") not in ("0", "false", "False")


def _book_pnl(p: dict, exit_px: float) -> float:
    """Close the trade on `p`: set pnl (gross), cost and pnl_net. Returns net.

    Cost is charged on the exit notional (size in USDT terms), matching how the
    live fills behaved: a round trip costs PAPER_COST_RT of the position's value.
    """
    size = p.get("size", 5.0)
    entry = p.get("entry") or 0.0
    qty = size / entry if entry else 0.0
    gross = (exit_px - entry) * qty
    p["pnl"] = round(gross, 4)
    if PAPER_CHARGE_COST and size:
        cost = size * PAPER_COST_RT
        p["cost"] = round(cost, 6)
        p["pnl_net"] = round(gross - cost, 4)
    else:
        p["cost"] = 0.0
        p["pnl_net"] = round(gross, 4)
    return p["pnl_net"]


def _paper_open(sig: dict, regime: str | None = None) -> None:
    """Open a PAPER position on a VALIDATED signal.

    `regime` (added 2026-10-10): the Judge computes a regime every cycle but the
    paper openers never persisted it, so `by_regime` collapsed to a single "?"
    bucket holding the whole book (0/2778 trades carried a regime) and the
    improvement loop could not read per-regime decay AT ALL. Stamping it here is
    the one-line fix for that data gap — same shape as the edge tag added the
    same day. Optional/defaulted so no existing caller or test breaks.
    """
    if _symbol_disabled(sig.get("symbol", "")):
        return
    if _edge_disabled("validated"):
        return
    s = {}
    if PAPER_STATE.exists():
        try:
            s = json.loads(PAPER_STATE.read_text())
        except Exception:
            s = {}
    s.setdefault("open", [])
    s.setdefault("closed", [])
    # no dup (symbol+tf) open
    if any(p["symbol"] == sig["symbol"] and p.get("tf") == sig["tf"] for p in s["open"]):
        return
    # INITIAL STOP 0.010 -> 0.020 (2026-09-16, OOS exit study): replaying the 508
    # native paper entries on real 5m bars showed 1.0-1.5% stops sit INSIDE the
    # noise band — with the 1.0% stop 158/508 exits were full stop-outs and only
    # 69 reached TP; at 2.0% only 65 stop out and 84 exit +0.8% (price touches
    # +0.5% first in 84% of trades, so the breakeven lock does the work).
    # Paired, same-harness, split-half: +/- +1.896 IS / +1.811 OOS (t=+3.58),
    # positive in 13/14 symbols. Envelope-safe: stop 0.020 <= 0.030 hard cap.
    # RR is UNCHANGED (stop*2) so this is ONE risk threshold, not a new plan.
    # Stop now comes from the SAME calibration the REAL book uses, so the paper
    # track cannot silently drift from live again. Fallback keeps the 2% value.
    # History (2026-09-20): paper was widened 1% -> 2% on 2026-09-16 on a paired
    # split-half study (t=+3.58, positive in 13/14 symbols) while REAL money kept
    # the 1% stop for four more days and kept bleeding. A validated fix that never
    # reached live. Both tracks now read data/exit-params.json.
    try:
        from live_executor import _exit_params
        stop = _exit_params()["stop_pct"]
    except Exception:  # noqa: BLE001
        stop = 0.02
    tp = round(sig["close"] * (1 + stop * 2), 4)
    sl = round(sig["close"] * (1 - stop), 4)
    s["open"].append({
        "symbol": sig["symbol"], "tf": sig["tf"], "entry": round(sig["close"], 4),
        "tp": tp, "sl": sl, "size": 5.0, "edge": "validated",
        "regime": regime or "?",
        "ts_utc": datetime.now(timezone.utc).isoformat(),
    })
    PAPER_STATE.write_text(json.dumps(s, indent=2))
    print(f"  PAPER OPEN {sig['symbol']} @ {sig['close']:.4f} TP {tp} SL {sl}")


def _paper_check(ex) -> None:
    """Close paper positions on TP/SL/time-stop (parallel learning track).
    TIME-STOP (6h): mirrors the REAL 6h hold. RAISED from 2h on 2026-09-06 —
    data showed the 2h stop was cutting 66 GREEN trades short before they could
    reach TP (109 TIME exits total, 66 green). Longer hold = setups actually
    get to run to target; matches live_executor's HOLD_HOURS=6.
    """
    if not PAPER_STATE.exists():
        return
    s = json.loads(PAPER_STATE.read_text())
    now = datetime.now(timezone.utc)
    keep = []
    for p in s["open"]:
        try:
            px = float(ex.fetch_ticker(p["symbol"])["last"])
        except Exception:
            keep.append(p)
            continue
        # RECORD THE ADVERSE EXTREME (2026-10-05). `best_px` records how far a trade
        # went in its FAVOUR; nothing recorded how far it went AGAINST. That gap made
        # every initial-stop question unanswerable: to ask "would a 1.2% stop have been
        # hit?" you need the worst price reached, and the book only stored the best.
        # `worst_px` is sampled from the same observed ticks as best_px (at the
        # :16/:46 scan cadence), so it is a lower bound on the true adverse extreme —
        # a tighter stop may have been breached between samples. Recorded for new
        # trades only; existing closed records keep their shape.
        w = float(p.get("worst_px") or p["entry"])
        if px < w:
            p["worst_px"] = round(px, 6)
        if px >= p["tp"]:
            _book_pnl(p, px)
            p["reason"] = "TP"
            s["closed"].append(p)
            print(f"  PAPER CLOSE {p['symbol']} TP pnl={p['pnl']:+.2f} "
                  f"net={p['pnl_net']:+.2f}")
        elif px <= p["sl"]:
            _book_pnl(p, px)
            p["reason"] = "SL"
            s["closed"].append(p)
            print(f"  PAPER CLOSE {p['symbol']} SL pnl={p['pnl']:+.2f} "
                  f"net={p['pnl_net']:+.2f}")
        else:
            # BREAKEVEN/TRAIL PROTECTION (mirrors live_executor 2026-09-07):
            # never give back a winner — >= +0.5% locks breakeven, >= +1.5%
            # trails SL to +1.2% banked.
            # TRAIL LOCK 0.8% -> 1.2% (2026-09-16 13Z, OOS exit study pass 2):
            # the live paper manage path only samples the OBSERVED price at the
            # :16/:46 scan, so a +0.8% lock was clipping winners 0.4% early;
            # replaying all 529 native paper entries on real 5m bars at the live
            # cadence (harness reproduces the recorded exit mix 49.5% vs 50.5% SL
            # and today's 13 post-2%-stop exits reason-for-reason, net +$0.014)
            # a +1.2% lock is +0.949 IS +0.201 / OOS +0.748, t=+2.70, positive in
            # ALL FOUR time quarters and in 7/14 symbols (worst -0.055); the
            # 1.1-1.5% plateau is positive both halves, so this is not a spike.
            # ONE threshold; initial stop (2%), RR (2.0/1.5), max_open, size and
            # the REAL-money live_executor trail are untouched.
            pnl_pct = (px - p["entry"]) / p["entry"] * 100
            if pnl_pct >= 1.5 and p.get("sl", 0) < p["entry"] * 1.012:
                p["sl"] = round(p["entry"] * 1.012, 4)
            elif pnl_pct >= 0.5 and p.get("sl", 0) < p["entry"]:
                p["sl"] = round(p["entry"], 4)
            # MOMENTUM-STALL EXIT (mirrors live_executor 2026-09-09): green but
            # no new high in ~90 min → bank profit, free slot for next signal.
            best = float(p.get("best_px") or p["entry"])
            if px > best:
                p["best_px"] = round(px, 6)
                p["best_ts_utc"] = now.isoformat()
                best = px
            stall = False
            if pnl_pct >= 0.3 and best > p["entry"] * 1.003:
                bts = p.get("best_ts_utc")
                if bts:
                    try:
                        if (now - datetime.fromisoformat(bts)).total_seconds() >= 5400:
                            stall = True
                    except Exception:
                        pass
            if stall:
                _book_pnl(p, px)
                p["reason"] = "STALL"
                p["exit_px"] = px
                s["closed"].append(p)
                print(f"  PAPER CLOSE {p['symbol']} STALL (no new high 90m) pnl={p['pnl']:+.2f} slot freed")
                continue
            # TIME-STOP: 4h without resolving -> close at market, free the slot
            age_h = None
            try:
                ts = p.get("ts_utc") or p.get("open_ts_utc")
                if ts:
                    age_h = (now - datetime.fromisoformat(ts)).total_seconds() / 3600.0
            except Exception:
                age_h = None
            # TIME-STOP with WINNER-PROTECTION: only free slots that are NOT
            # meaningfully green. A green position (up >= +0.5%) keeps running
            # toward TP up to 12h — dumping it at +0.3% was the #1 profit leak
            # (72 trades exited green at avg +0.31% instead of reaching TP
            # +2.58%; wins averaged +0.63% vs losses -0.84% = negative EV even
            # at 55% WR). Flat/red positions still time-exit at 6h to recycle.
            # Hold now sourced from the same calibration as REAL money.
            # NOTE: the "2h cuts green trades short -> use 6h" decision of
            # 2026-09-06 optimised the WRONG metric (how many trades REACH TP).
            # A joint stop x hold grid on 30 real trades (2026-09-20) shows 2h
            # beats 6h at EVERY stop width, and the 6h column is negative at every
            # stop width (-0.28% to -0.44%/trade). Cutting a green trade at 2h can
            # still be correct when the alternative is giving the move back.
            try:
                from live_executor import _exit_params
                _hold_h = _exit_params()["hold_hours"]
            except Exception:  # noqa: BLE001
                _hold_h = 6.0
            if age_h is not None and age_h >= _hold_h:
                pnl_pct = (px - p["entry"]) / p["entry"] * 100
                if pnl_pct >= 0.5 and age_h < 12.0:
                    keep.append(p)  # winner still running — DON'T cut it short
                    continue
                _book_pnl(p, px)
                p["reason"] = "TIME"
                p["exit_px"] = px
                s["closed"].append(p)
                print(f"  PAPER CLOSE {p['symbol']} TIME-STOP {age_h:.0f}h pnl={p['pnl']:+.2f} (slot freed)")
            else:
                keep.append(p)
    s["open"] = keep
    PAPER_STATE.write_text(json.dumps(s, indent=2))


def _pullback_open(sig: dict, regime: str | None = None) -> bool:
    """UPTREND PULLBACK edge — fits grinding/rising markets where dips are small.
    Entry: STRONG uptrend + small pullback + RSI 45-62 (see _is_pullback).
    Paper always; real when trade-all-signals is on (owner mandate).
    """
    if not _is_pullback(sig):
        return False
    if _edge_disabled("pullback"):
        return False
    s = {}
    if PAPER_STATE.exists():
        try:
            s = json.loads(PAPER_STATE.read_text())
        except Exception:
            s = {}
    s.setdefault("open", [])
    s.setdefault("closed", [])
    # one paper position per (symbol+tf) max — a 5m and a 30m signal on the
    # same symbol are DIFFERENT setups, so both may run. Cap total at 8.
    if any(p["symbol"] == sig["symbol"] and p.get("tf") == sig["tf"] for p in s["open"]):
        return False
    if len(s["open"]) >= 8:
        return False
    stop = 0.012  # 1.2% paper stop
    tp = round(sig["close"] * (1 + stop * 1.5), 6)
    sl = round(sig["close"] * (1 - stop), 6)
    s["open"].append({
        "symbol": sig["symbol"], "tf": sig["tf"], "entry": round(sig["close"], 6),
        "tp": tp, "sl": sl, "size": 5.0, "loose": True, "edge": "pullback",
        "regime": regime or "?",
        "ts_utc": datetime.now(timezone.utc).isoformat(),
    })
    PAPER_STATE.write_text(json.dumps(s, indent=2))
    print(f"  PULLBACK OPEN {sig['symbol']} @ {sig['close']:.4f} TP {tp} SL {sl}")
    return True


def _symbol_disabled(sym: str) -> bool:
    """True if this SYMBOL is on the autonomous disable list in
    data/signals-state.json (key 'disabled_symbols').

    SELF-IMPROVE DISABLE LIST (2026-09-30): mirrors the existing edge-disable
    mechanism (see _imported_strategy_open). An edge disable stops a STRATEGY;
    this stops a SYMBOL that has flipped from proven-winner to persistent
    bleeder across a split-half OOS test. ARB/USDT is the first entry:
    30d split-half IS +3.70 (WR 46%) / OOS -1.20 (WR 37%), and the last 7d
    was n=45 WR 26.7% net -1.171 (median pnl -0.010, net still -1.71 with the
    top-3 winners removed) -> a real regime-shift loser, not one-half noise.
    Paper-only impact: ARB is never real (not in ELITE/REAL_TIER); removing it
    frees a scarce paper slot for NEAR/JUP, which pass the same OOS test.
    """
    try:
        st = ROOT / "data" / "signals-state.json"
        if st.exists():
            return sym in json.loads(st.read_text()).get("disabled_symbols", [])
    except Exception:  # noqa: BLE001
        pass
    return False


def _edge_disabled(edge: str) -> bool:
    """True if this EDGE is on the autonomous disable list in
    data/signals-state.json (key 'disabled').

    🔴 FIX (2026-10-12): the self-improve engine writes proven-losing edges to
    `disabled`, and `_imported_strategy_open` has always honoured that list —
    but the THREE NATIVE openers (`validated`, `loose`, `pullback`) never read
    it. So when the engine disabled edge 'loose' on 2026-10-11 02:40, loose
    paper positions kept opening: 44 closed AFTER the disable and 4 were still
    open at audit time. The disable was COSMETIC for every native edge — the
    exact "disabled strategies actually stop opening (not cosmetic)" defect the
    imported-track comment claims was fixed. This is the single reader all
    openers must go through so an edge disable is enforced everywhere.
    """
    if not edge:
        return False
    try:
        st = ROOT / "data" / "signals-state.json"
        if st.exists():
            return edge in json.loads(st.read_text()).get("disabled", [])
    except Exception:  # noqa: BLE001 — a bad state file must not block trading
        pass
    return False


def _is_loose(sig: dict) -> bool:
    """Pure loose-signal test (no side effects) — used by scan loop for REAL
    firing, and by _paper_loose_open. Keeps real-vs-paper gates identical.
    Thresholds (dip scaled by timeframe) — WIDENED 2026-09-05 (owner chose
    max throughput):
        5m : dip<=-0.6%  rsi<=58   (many chances/day)
        15m: dip<=-1.1%  rsi<=58
        30m: dip<=-1.6%  rsi<=58
    """
    tf = sig.get("tf", "15m")
    dip_bar = {"5m": -0.006, "15m": -0.011, "30m": -0.016}.get(tf, -0.011)
    return bool(sig["dd"] <= dip_bar and sig["rsi"] <= 58)


def _is_sharp(sig: dict) -> bool:
    """SHARP signal (2026-09-09 backtest): RSI<=35 deep-capitulation dip on
    15m = 87-93% WR (ETH 87%, XRP 93%, SOL 90% over 10-15 trades each).
    The loose rsi<=58 rule measured just 45-49% — barely above coin-flip —
    so REAL money should prefer SHARP setups. LINK is a trap even sharp
    (35% WR) — excluded by tier gate."""
    tf = sig.get("tf", "15m")
    if tf != "15m":
        return False  # reliable sample only exists on 15m
    return bool(sig["dd"] <= -0.011 and sig["rsi"] <= 35)


def _is_pullback(sig: dict) -> bool:
    """DEMOTED 2026-09-06: pullback edge KILLED — 35% WR, -$1.03 net over 17
    trades (worst edge by far). Buying dips in 'uptrends' kept catching tops
    that rolled over. Always returns False: no paper, no real. Data verdict:
    the dip-buy (validated/loose) + mean-reversion (rsi2) edges are the winners.
    """
    return False



def _paper_loose_open(sig: dict, regime: str | None = None) -> bool:
    """Open a PAPER+REAL position on a shallower dip that does NOT meet the strict
    real-money validated bar. Timeframe-aware thresholds so SHORT timeframes fire
    OFTEN (5m dips happen many times/day; 30m deep dips are rare). Owner mandate:
    trade frequently + aggressively, keep learning. Positive paper evidence
    (52% WR, +$0.49 net over 23 trades) justifies real money on these too.
    """
    if not _is_loose(sig):
        return False
    if _symbol_disabled(sig.get("symbol", "")):
        return False
    if _edge_disabled("loose"):
        return False
    s = {}
    if PAPER_STATE.exists():
        try:
            s = json.loads(PAPER_STATE.read_text())
        except Exception:
            s = {}
    s.setdefault("open", [])
    s.setdefault("closed", [])
    # one paper position per symbol max, and cap total paper positions at 8
    if any(p["symbol"] == sig["symbol"] and p.get("tf") == sig["tf"] for p in s["open"]):
        return False
    if len(s["open"]) >= 8:
        return False
    # INITIAL STOP 0.015 -> 0.020 (2026-09-16, same OOS exit study as _paper_open).
    # Loose-only subset (n=154): +0.216 IS / +0.326 OOS vs the 1.5% stop, i.e.
    # positive in both halves but weaker evidence (t=+1.45) — revert this one
    # alone if a stricter single-variable policy is preferred. RR unchanged.
    stop = 0.02  # 2% paper stop (was 1.5%; OOS-validated 2026-09-16)
    tp = round(sig["close"] * (1 + stop * 1.5), 6)
    sl = round(sig["close"] * (1 - stop), 6)
    s["open"].append({
        "symbol": sig["symbol"], "tf": sig["tf"], "entry": round(sig["close"], 6),
        "tp": tp, "sl": sl, "size": 5.0, "loose": True, "edge": "loose",
        "regime": regime or "?",
        "ts_utc": datetime.now(timezone.utc).isoformat(),
    })
    PAPER_STATE.write_text(json.dumps(s, indent=2))
    print(f"  PAPER(loose) OPEN {sig['symbol']} @ {sig['close']:.4f} TP {tp} SL {sl}")
    return True


def _imported_strategy_open(sig: dict, regime: str | None = None) -> bool:
    """Open a PAPER position for an IMPORTED public strategy (owner: copy other
    people's strategies + trade in parallel). Paper-only: each strategy's edge
    tag ('supertrend','rsi2','bollinger','ema-cross') flows into the
    improvement_loop per-edge stats, which promote proven winners to real.
    `regime` (2026-10-10) is stamped for per-regime attribution, same as the
    native openers.
    """
    s = {}
    if PAPER_STATE.exists():
        try:
            s = json.loads(PAPER_STATE.read_text())
        except Exception:
            s = {}
    s.setdefault("open", [])
    s.setdefault("closed", [])
    edge = sig.get("edge", "imported")
    # SELF-IMPROVE DISABLE LIST (2026-09-12): the autonomous engine writes
    # proven-losing edges to data/signals-state.json; respect it here so
    # disabled strategies actually stop opening (not cosmetic).
    try:
        _st = ROOT / "data" / "signals-state.json"
        if _st.exists():
            _disabled = json.loads(_st.read_text()).get("disabled", [])
            if edge in _disabled:
                return False
    except Exception:
        pass
    # one position per (symbol+tf+strategy) — AND max 2 imported positions per
    # symbol total (else BTC/ETH flood the board and starve the native edges)
    if any(p["symbol"] == sig["symbol"] and p.get("tf") == sig["tf"]
           and p.get("edge") == edge for p in s["open"]):
        return False
    imp_on_sym = sum(1 for p in s["open"] if p["symbol"] == sig["symbol"]
                     and p.get("imported"))
    if imp_on_sym >= 2:
        return False
    # imported total cap 6 — ALWAYS leaves >=4 slots for the native edges
    imp_total = sum(1 for p in s["open"] if p.get("imported"))
    if imp_total >= 6:
        return False
    if len(s["open"]) >= 10:
        return False
    stop = 0.012  # 1.2% paper stop
    tp = round(sig["close"] * (1 + stop * 1.8), 6)
    sl = round(sig["close"] * (1 - stop), 6)
    s["open"].append({
        "symbol": sig["symbol"], "tf": sig["tf"], "entry": round(sig["close"], 6),
        "tp": tp, "sl": sl, "size": 5.0, "edge": edge, "imported": True,
        "regime": regime or "?",
        "ts_utc": datetime.now(timezone.utc).isoformat(),
    })
    PAPER_STATE.write_text(json.dumps(s, indent=2))
    print(f"  IMPORTED-{edge} OPEN {sig['symbol']} {sig['tf']} @ {sig['close']:.4f} TP {tp} SL {sl}")
    return True


def _ordered_syms(ex=None) -> list:
    """Symbols in scan priority — ADAPTIVE TREND RANKING (2026-09-06).
    Each scan ranks the universe by 24h momentum (strongest first) so the coins
    ACTUALLY trending up claim slots first, instead of a hardcoded list. This
    stops the XRP failure mode: buying dips in a coin sitting at the BOTTOM of
    its range (XRP was at 0% of 24h range while SOL +2.7% / LINK +2.0% trended).
    Falls back to the static order if the exchange call fails.
    """
    if ex is not None:
        try:
            scored = []
            for s in SYMS:
                try:
                    o = ex.fetch_ohlcv(s, "1h", limit=25)
                    if len(o) >= 2:
                        c0 = o[0][4]
                        c1 = o[-1][4]
                        chg24 = (c1 / c0 - 1) * 100 if c0 else 0
                        scored.append((s, chg24))
                except Exception:
                    continue
            if scored:
                # strongest momentum first; keep real-pair coverage by demoting
                # only clear losers (not excluding — rotation still covers all)
                scored.sort(key=lambda x: -x[1])
                return [s for s, _ in scored] + [s for s in SYMS if s not in
                                                 [x[0] for x in scored]]
        except Exception:
            pass
    rest = [s for s in SYMS if s not in CORE_SYMS]
    return CORE_SYMS + rest


def _ordered_tfs() -> list:
    """Timeframes in scan priority: 5m/15m (the 69%/60% WR earners) first,
    30m (coin-flip 52%) last so it only fires when slots are still free.
    """
    rest = [t for t in TFS if t not in CORE_TFS]
    return CORE_TFS + rest


def _market_breadth(ex) -> dict:
    """% of the universe down over 24h. >=60% down = risk-off bleed."""
    up = down = 0
    try:
        for s in SYMS:
            try:
                o = ex.fetch_ohlcv(s, "1h", limit=25)
                if len(o) >= 2:
                    chg = (o[-1][4] / o[0][4] - 1) * 100
                    if chg > 1:
                        up += 1
                    elif chg < -1:
                        down += 1
            except Exception:
                continue
    except Exception:
        pass
    total = up + down
    return {"up": up, "down": down,
            "down_pct": (down / total * 100) if total else 0}


def main():
    from trading.exchange.live_client import make_live_exchange, live_balances
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from trade_judge import decide as judge_decide
    ex = make_live_exchange()
    # RELIABILITY FIX (2026-10-01): load_markets() hits Binance's authenticated
    # sapi/capital/config/getall endpoint. Originally (2026-09-30) we retried 3x
    # and, on total failure, returned EARLY. That was still a blackout bug: the
    # endpoint was down for ~14 CONSECUTIVE cron cycles (logs/unified.log:1239-1291,
    # ~7 hours) and every one of those cycles skipped the ENTIRE scan — including
    # _paper_check() position management and futures_manage(), so open live/paper/
    # futures positions got ZERO TP/SL/trail supervision for 7 hours.
    #
    # load_markets() is NOT required for any of that: paper management uses public
    # fetch_ohlcv/fetch_ticker, and the real-order path (live_executor.open_long ->
    # place_market_buy) creates its OWN exchange client and loads markets itself.
    # So make it NON-FATAL: on failure, warn, mark markets as DEGRADED (which blocks
    # only the real-order path via _real_can_trade), and CONTINUE the scan so paper
    # learning + position management + futures keep running.
    global _MARKETS_OK
    _markets_ok = False
    for _attempt in range(3):
        try:
            ex.load_markets()
            _markets_ok = True
            break
        except Exception as _e:
            print(f"WARN: load_markets attempt {_attempt + 1}/3 failed: {str(_e)[:120]}")
            if _attempt < 2:
                time.sleep(2 * (_attempt + 1))
    _MARKETS_OK = _markets_ok
    if not _markets_ok:
        # Fail SOFT, not fatal: continue with paper + position management + futures.
        # Real orders are blocked this cycle (degraded) until markets reload.
        print("WARN: exchange markets unavailable after 3 attempts — DEGRADED cycle: "
              "paper + position management + futures continue; REAL orders skipped")

    # 0) THE JUDGE: regime-aware decision (the "human PM" layer).
    b = live_balances()
    free = float(b.get("USDT", 0)) + float(b.get("USD", 0)) + float(b.get("USDC", 0))
    verdict = judge_decide(ex, free)

    # 0b) MARKET-BREADTH RISK GATE (2026-09-08): if >=60% of the universe is
    # DOWN over 24h, it's a market-wide bleed — dip-buying catches falling
    # knives (today: 6/10 coins down, LINK -2.9%, every dip-loss was a knife).
    # Gate: REAL dip-buys stand down in a bleed; paper keeps learning.
    # AVAX/SUI-type momentum (coin up while market down) still allowed.
    breadth = _market_breadth(ex)
    risk_off = breadth.get("down_pct", 0) >= 60.0
    print(f"BREADTH: {breadth.get('up')}up/{breadth.get('down')}down "
          f"({breadth.get('down_pct'):.0f}% down) → {'RISK-OFF stand down' if risk_off else 'normal'}")

    # OWNER OVERRIDE: TRADE_ALL_SIGNALS=1 means the owner explicitly opened the
    # gate for real money on every signal. The Judge's tier-caution is advisory
    # when this flag is on — the owner's explicit instruction wins. Hard risk
    # caps (1 pos, 1% stop, $1 daily loss) still apply in live_executor.
    from live_executor import TRADE_ALL_SIGNALS
    if TRADE_ALL_SIGNALS:
        verdict["real_allowed"] = True
        verdict["reason"] += " | OWNER OVERRIDE: trade all signals real"
    print(f"JUDGE: {verdict['action']} | regime={verdict['regime']['regime']} "
          f"| hunt={verdict.get('hunt')} | real={verdict.get('real_allowed')}")
    hunt_edge = verdict.get("hunt")  # which edge the Judge says to prioritize
    # The Judge's regime for THIS cycle — stamped onto every paper trade opened
    # below so the improvement loop can attribute decay per regime (fixes the
    # 0/2778 regime gap; see _paper_open docstring).
    regime = (verdict.get("regime") or {}).get("regime", "?")

    # 0c) FUTURES LEG (2026-09-09): same elite signals (15m SHARP on SOL/XRP),
    # executed on USDT-M futures at 3x with hard caps. Check/manage open futures
    # positions first (TP/SL/breakeven/stall), then scan for new entries.
    futures_events = []
    try:
        from futures_executor import futures_manage, futures_open, SPOT_TO_FUT
        futures_events = futures_manage().get("events", [])
    except Exception as e:
        futures_events = [f"futures manage skipped: {str(e)[:80]}"]

    # 1) Always run paper track in parallel (check closes + open new on signals)
    _paper_check(ex)
    paper_fired = 0

    # 1b) IMPORTED PUBLIC STRATEGIES (paper-parallel learning track): scan the
    # same universe with Supertrend / RSI(2) / Bollinger / EMA-cross. Paper-only
    # by design — the improvement loop promotes proven winners to real.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from strategies_import import all_strategies as _all_imported
        for sym in _ordered_syms(ex):
            for tf in _ordered_tfs():
                for isig in _all_imported(ex, sym, tf):
                    if _imported_strategy_open(isig, regime):
                        paper_fired += 1
    except Exception as e:  # imported-track must never break the main loop
        print(f"  (imported strategies pass skipped: {str(e)[:80]})")

    # 2) Scan for signals across all sym/tf (trend-ranked: strongest first)
    fired_real = []
    for sym in _ordered_syms(ex):
        for tf in _ordered_tfs():
            sig = _features(ex, sym, tf)
            if not sig:
                continue
            edge = _validated(sig)
            # SHARP tag: deep-capitulation 15m signal (87-93% WR backtest) —
            # flagged for the adaptive-plan confidence boost in live_executor.
            if _is_sharp(sig):
                sig["sharp"] = True
            # FUTURES LEG (2026-09-10 FIX): independent of the strict `edge`
            # gate. Futures trades the SHARP condition (dip<=-1.1%, rsi<=35) on
            # SOL/XRP at 3x. Previously nested under `if edge:` (which needs
            # dip<=-2%, rsi<=30, red>=3) -> fired ~never -> futures silent.
            # risk_off guard: skip futures during a market-wide bleed (the
            # hard-won lesson: regime beats signal).
            if (sig.get("sharp") and sym in ("SOL/USDT", "XRP/USDT")
                    and not risk_off):
                try:
                    fr = futures_open(sym, sig)
                    if fr.get("ok"):
                        futures_events.append(
                            f"FUTURES OPEN {sym} @ {fr['pos']['entry']} "
                            f"notional ${fr['pos']['notional']} 3x")
                    elif "already" not in fr.get("err", ""):
                        futures_events.append(f"futures skip {sym}: {fr.get('err','')[:60]}")
                except Exception as fe:
                    futures_events.append(f"futures open err: {str(fe)[:80]}")
            # On a VALIDATED signal: fire BOTH paper and real in parallel.
            if edge:
                paper_took = False
                if not any(p["symbol"] == sym and p.get("tf") == tf for p in
                           (json.loads(PAPER_STATE.read_text()).get("open", []) if PAPER_STATE.exists() else [])):
                    _paper_open(sig, regime)
                    paper_fired += 1
                    paper_took = True
                real_took = False
                # RISK-OFF GATE: in a market-wide bleed, only coins that are
                # themselves UP (momentum outliers) get real money.
                # ELITE GATE: real money = 15m SHARP on ETH/SOL/XRP only.
                sym_mom = sig.get("mom", 0)
                if (_real_can_trade() and verdict.get("real_allowed", False)
                        and _real_tier_ok(sym, risk_off, sig, ex)
                        and (not risk_off or sym_mom > 0)):
                    r = _real_open(sym, sig)
                    if r.get("ok"):
                        fired_real.append(sym)
                        real_took = True
                # FUTURES LEG handled above (independent sharp branch) — the
                # old nested copy was removed 2026-09-10 to stop double-firing.
                if paper_took and real_took:
                    print(f"  >>> PARALLEL FIRE {sym} {tf}: PAPER + REAL both opened")
                elif paper_took:
                    print(f"  >>> PAPER only {sym} {tf} (real skipped: {_real_skip_reason(sym)})")
            # LOOSE track: paper always; real INDEPENDENT of paper slot state.
            elif _is_loose(sig):
                # real first — must NOT depend on paper-slot availability.
                # ELITE GATE applies: loose (non-sharp) dips never qualify for
                # real money anymore — paper only. Kept for future re-tuning.
                if (TRADE_ALL_SIGNALS and verdict.get("real_allowed", False)
                        and _real_can_trade()
                        and _real_tier_ok(sym, risk_off, sig, ex)
                        and (not risk_off or sig.get("mom", 0) > 0)):
                    r = _real_open(sym, sig)
                    if r.get("ok"):
                        fired_real.append(sym)
                        print(f"  >>> LOOSE REAL {sym} {tf}: real opened (all-signals mode)")
                if _paper_loose_open(sig, regime):
                    paper_fired += 1
            # PULLBACK edge: uptrend small-dip — fits grinding markets. Paper always.
            elif _is_pullback(sig):
                if TRADE_ALL_SIGNALS and verdict.get("real_allowed", False) and _real_can_trade():
                    r = _real_open(sym, sig)
                    if r.get("ok"):
                        fired_real.append(sym)
                        print(f"  >>> PULLBACK REAL {sym} {tf}: real opened (all-signals mode)")
                if _pullback_open(sig, regime):
                    paper_fired += 1

    print(f"scan done: paper signals fired={paper_fired}, real fired={len(fired_real)}")
    if fired_real:
        print("REAL TRADES:", ", ".join(fired_real))
    for ev in futures_events:
        print(f"  FUTURES: {ev}")


def _real_skip_reason(sym: str) -> str:
    """Human-readable reason real money didn't fire (for logging)."""
    try:
        from live_executor import live_enabled, LIVE_MAX_OPEN, _load as _lload, ALLOWED_REAL
        if not live_enabled():
            return "live gate closed"
        if len(_lload()["open"]) >= LIVE_MAX_OPEN:
            return "max 1 real position open"
        if sym not in ALLOWED_REAL:
            return f"{sym} fails lot-rounding at $6 min (use XRP/SOL/DOGE/ADA/LINK/AVAX/BNB/SUI/TON/TRX)"
        from trading.exchange.live_client import live_balances
        b = live_balances()
        free = float(b.get("USDT", 0)) + float(b.get("USD", 0)) + float(b.get("USDC", 0))
        if free < 6.0:
            return f"insufficient free ({free:.2f} < 6.0)"
    except Exception as e:
        return f"err:{e}"
    return "unknown"


def _htf_trend_ok(ex, sym: str) -> bool:
    """HTF TREND FILTER (2026-09-10) — the ONE filter that tested positive.

    Backtest finding: adding a higher-timeframe check (1h trend not collapsing)
    lifted the dip signal from -0.45% to +0.02% net (WR 47.1% -> 51.9%) in the
    only experiment that showed improvement. Regime matters more than signal.

    Rule: allow a dip-buy only if the 1h close is NOT below its 1h EMA50
    (i.e. the larger trend hasn't rolled over). In a broad bleed, dips keep
    dipping — this filter blocks those.
    Returns True (allowed) if data is unavailable, to avoid over-blocking.
    """
    try:
        o = ex.fetch_ohlcv(sym, '1h', limit=80)
        if not o or len(o) < 55:
            return True
        closes = [c[4] for c in o]
        k = 2 / (50 + 1)
        e = closes[0]
        for v in closes[1:]:
            e = v * k + e * (1 - k)
        # require close above 1h EMA50 (uptrend intact)
        return closes[-1] >= e
    except Exception:
        return True


def _real_tier_ok(sym: str, risk_off: bool = False, sig: dict | None = None,
                  ex=None) -> bool:
    """SYMBOL TIER GATE (2026-09-09): real money only on ELITE high-confidence
    setups — 15m SHARP signals on ETH/SOL/XRP.
    HTF FILTER (2026-09-10): additionally require the 1h trend to be intact
    (the only filter that tested positive across all experiments).
    Everything else (loose dips, memes, non-elite symbols) = paper only.
    risk_off further requires the coin itself to have momentum (checked by caller).

    🔴 FIX (2026-10-12): the self-improve SYMBOL disable (`disabled_symbols` in
    data/signals-state.json) was enforced on the PAPER openers but NOT here — so
    a symbol the system had stood down could still take REAL money. This was
    latent while the list held only ARB (never ELITE); it became live the moment
    SOL/USDT was added, because SOL IS in ELITE_SYMS. Evidence: the real book has
    two SOL closes on 2026-10-12 (04:16 STALL +0.0693, 11:56 TIME -0.018) while
    SOL sat on the disabled list. A disable that the money path ignores is
    cosmetic — same defect class as the edge-disable fixed the same day.
    This is the single chokepoint every real-order call site already passes
    through (lines ~840/858/870), so one check covers all of them."""
    if sym not in ELITE_SYMS:
        return False
    if _symbol_disabled(sym):
        return False
    if sig is not None:
        if not sig.get("sharp"):
            return False
        # HTF trend filter (only when ex provided)
        if ex is not None and not _htf_trend_ok(ex, sym):
            return False
        return True
    return True


def _real_can_trade() -> bool:
    # DEGRADED MODE (2026-10-01): if markets failed to load this cycle, do not
    # attempt real orders (precision/lot-rounding cache is stale/absent).
    # Paper + management still run; real resumes automatically next healthy cycle.
    if not _MARKETS_OK:
        return False
    try:
        from live_executor import live_enabled, LIVE_MAX_OPEN, _load as _lload
        if not live_enabled():
            return False
        if len(_lload()["open"]) >= LIVE_MAX_OPEN:
            return False
        from trading.exchange.live_client import live_balances
        b = live_balances()
        free = float(b.get("USDT", 0)) + float(b.get("USD", 0)) + float(b.get("USDC", 0))
        return free >= 6.0  # buffer check
    except Exception:
        return False


def _real_open(sym: str, sig: dict) -> dict:
    # Real money only on pairs that survive lot-rounding at $6 min (see ALLOWED_REAL).
    try:
        from live_executor import open_long, ALLOWED_REAL
        if sym not in ALLOWED_REAL:
            return {"ok": False, "reason": f"{sym} skipped for real (lot rounding)"}
        # pass the FULL signal so open_long can compute adaptive TP/SL/size
        # from ATR volatility + momentum + trend (2026-09-06)
        return open_long(sym, None, None, sig)
    except Exception as e:
        return {"ok": False, "reason": str(e)[:120]}


if __name__ == "__main__":
    main()
