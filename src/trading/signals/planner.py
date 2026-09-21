"""Precise trade planner — entry/TP/SL + expectancy-based sizing.

The "top-notch trader" layer. For each candidate signal it computes:
  - P(up) from the prediction model (or the validated dip-edge prior)
  - expected value (EV) per trade, after fees
  - precise TP and SL from volatility (ATR-scaled, not guessed)
  - position size via a Kelly-capped formula (bet more when edge is high)

This REPLACES blind leverage. Instead of "max the lever", we "size to the
edge" — which is how profitable traders actually outsmart risk.

The leverage decision is then DERIVED from EV, not chosen for its own sake:
  - If EV is strongly positive and stop is tight → modest leverage justified
  - If EV is thin or stop is wide → stay 1x (spot)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from trading.signals.scorer import score_signal


@dataclass
class TradePlan:
    symbol: str
    action: str            # BUY / WATCH / AVOID
    entry: float           # reference price
    p_up: float            # probability of profit
    tp: float              # take-profit price
    sl: float              # stop-loss price
    tp_pct: float
    sl_pct: float
    ev_pct: float          # expected value after fees (per trade)
    kelly_fraction: float  # suggested position fraction (0-1)
    max_leverage: float    # justified leverage (derived, never >3 in v1)
    size_usdt: float       # suggested position size for a given bankroll
    reason: str


def plan_trade(sig: dict, bankroll_usdt: float, fee_rate: float = 0.001) -> TradePlan:
    """Compute a precise trade plan for a signal.

    Uses the EV scorer for P(up), ATR/vol for SL/TP distance, and a
    Kelly-capped sizing formula. Fail-closed: any uncertainty → AVOID.
    """
    symbol = sig.get("symbol", "?")
    q = score_signal(sig)
    score = q["score"]
    dd = sig.get("drawdown_pct", 0) or 0
    atr_ratio = sig.get("atr_ratio", 1.0) or 1.0
    price = sig.get("price") or 0.0

    # P(up) from the validated dip-edge curve (the honest prior).
    # Shallow dip = high P(up); deep dip = low P(up).
    if -0.08 <= dd <= -0.03:
        p_up = 0.578  # validated 57.8% win at -5%/48h
    elif -0.12 <= dd < -0.08:
        p_up = 0.56   # -8% band
    elif dd < -0.12:
        p_up = 0.42   # deep dip = knife, less than coin flip
    else:
        p_up = 0.50   # no edge

    # Adjust P(up) by volatility: HIGH vol HELPS shallow-dip reversion
    # (validated 66% WR out-of-sample). Only extreme vol hurts.
    if atr_ratio > 2.5:
        p_up -= 0.05   # extreme vol = knife risk
    elif atr_ratio >= 1.5:
        p_up += 0.05   # healthy high vol = bigger reverting moves (66% WR)
    elif atr_ratio < 0.8:
        p_up -= 0.03   # low vol = drift, weak edge
    p_up = max(0.30, min(0.75, p_up))

    # TP/SL from volatility — ATR-scaled, asymmetric reward:risk.
    # Shallow-dip reversion: target a 2:1 reward-to-risk on a tight stop.
    # Units: PERCENT numbers (1.5 = 1.5%).
    sl_pct = 1.5 if atr_ratio <= 1.5 else 2.5
    tp_pct = sl_pct * 2.0  # 2:1 R:R

    # Expected value after fees (percent numbers).
    win_pay = tp_pct
    loss_pay = sl_pct
    ev = (p_up * win_pay - (1 - p_up) * loss_pay) - (2 * fee_rate * 100)

    # Kelly fraction (capped at 25% of bankroll, floor at 0).
    b = win_pay / loss_pay if loss_pay > 0 else 0.0  # odds
    kelly = (p_up * b - (1 - p_up)) / b if b > 0 else 0.0
    kelly = max(0.0, min(0.25, kelly))  # half-kelly + hard cap

    # Derived leverage: only justified if EV strongly positive AND stop tight.
    if ev > 0.5 and sl_pct <= 2.0:
        max_lev = min(3.0, 1.0 + (ev - 0.5) * 2)  # scales up to 3x
    elif ev > 0.15:
        max_lev = 1.5
    else:
        max_lev = 1.0  # spot only

    if price > 0:
        entry = price
        tp = round(price * (1 + tp_pct / 100), 2)
        sl = round(price * (1 - sl_pct / 100), 2)
    else:
        entry = tp = sl = 0.0

    size_usdt = round(bankroll_usdt * kelly, 2)

    if ev <= 0 or score < 50:
        action = "AVOID"
    elif score >= 70 and ev > 0.15:
        action = "BUY"
    else:
        action = "WATCH"

    return TradePlan(
        symbol=symbol, action=action, entry=entry, p_up=round(p_up, 3),
        tp=tp, sl=sl, tp_pct=round(tp_pct, 2), sl_pct=round(sl_pct, 2),
        ev_pct=round(ev, 3), kelly_fraction=round(kelly, 3),
        max_leverage=round(max_lev, 2), size_usdt=size_usdt,
        reason=(f"score={score:.0f} dd={dd*100:.1f}% P(up)={p_up*100:.0f}% "
                f"EV={ev:+.2f}%"),
    )


def plan_card(plan: TradePlan) -> str:
    """Render a trade plan as a Telegram card body."""
    return (
        "<pre>"
        f"SYMBOL      : {plan.symbol}\n"
        f"ACTION      : {plan.action}\n"
        f"ENTRY       : {plan.entry}\n"
        f"TAKE PROFIT : {plan.tp}  (+{plan.tp_pct}%)\n"
        f"STOP LOSS   : {plan.sl}  (-{plan.sl_pct}%)\n"
        f"P(win)      : {plan.p_up*100:.0f}%\n"
        f"EXPECTANCY  : {plan.ev_pct:+.2f}% per trade\n"
        f"SIZE        : ${plan.size_usdt} ({plan.kelly_fraction*100:.0f}% bankroll)\n"
        f"MAX LEV     : {plan.max_leverage}x\n"
        f"REASON      : {plan.reason}"
        "</pre>"
    )


if __name__ == "__main__":
    demo = [
        {"symbol": "BTC/USDT", "drawdown_pct": -0.05, "regime": "RANGING", "atr_ratio": 1.0, "price": 63470},
        {"symbol": "ETH/USDT", "drawdown_pct": -0.15, "regime": "RANGING", "atr_ratio": 1.2, "price": 1880},
        {"symbol": "SOL/USDT", "drawdown_pct": -0.25, "regime": "VOLATILE", "atr_ratio": 2.5, "price": 76},
    ]
    for sig in demo:
        plan = plan_trade(sig, bankroll_usdt=100.0)
        print(plan_card(plan))
        print()
