"""In-position management, layer L1 (docs/loop/designs/V2-15): a deterministic ratchet on the best excursion.

Evidence (docs/v2/evidence/exit_study_paired.json, BTC/ETH/SOL 1m, 2023-2024 and 2025-2026 separately): of the
trades that reached HALF their target, 27-28% still ended as a LOSS under the plain bracket, which is the owner's
"went for TP, came back to SL" case. Locking half the best excursion once half the target is reached cut that to
0.0% (train) and 0.2% (test: gaps through the locked stop), and the mean return was not measurably different from
holding the bracket (-0.03% in both periods, 95% CIs spanning zero). The study's numbers are THE defaults below; the pre-registered
full study is reported alongside, including that no exit rule creates edge from edgeless entries.

Pure: no clock, no I/O. Called by the paper book at the CLOSE of every 1m bar; the stop it returns applies from the
NEXT bar (the same timing the study simulated, so the evidence matches what runs).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RatchetRule:
    activate_frac: float = 0.5     # arm once the best excursion reaches this fraction of the target distance
    lock_frac: float = 0.5         # then keep the stop at entry + lock_frac x best excursion
    version: str = "ratchet-lock-v1"

    def __post_init__(self):
        if not 0.0 < self.activate_frac <= 1.0:
            raise ValueError("activate_frac must be in (0, 1]")
        if not 0.0 < self.lock_frac < 1.0:
            raise ValueError("lock_frac must be in (0, 1): 1 would put the stop at the best price")


@dataclass(frozen=True)
class RatchetAction:
    kind: str          # "tighten": move the stop to `level`; "exit": the locked level is already at/through the
    level: float       # last price, so a stop there would trigger at once: close at the next open instead


def ratchet(rule: RatchetRule, *, side: str, entry: float, target: Optional[float], best: float,
            stop: float, last: float) -> Optional[RatchetAction]:
    """What the ratchet does at this bar's close, or None.

    side: LONG | SHORT; entry: fill price; target: take-profit level (None: the rule cannot arm);
    best: best price since entry (highest high for a long, lowest low for a short) INCLUDING this bar;
    stop: the stop in force; last: this bar's close. The level never loosens. A level at or through `last` is
    an EXIT, not a stop (an exchange refuses a stop that would trigger immediately; the study's simulator
    filled it at the next open, which is what an exit at the next open does).
    """
    if target is None:
        return None
    if side not in ("LONG", "SHORT"):
        raise ValueError(f"unknown side {side!r}")
    sign = 1.0 if side == "LONG" else -1.0
    dist = sign * (target - entry)
    exc = sign * (best - entry)
    if dist <= 0 or exc < rule.activate_frac * dist:
        return None
    level = entry + sign * rule.lock_frac * exc
    if sign * (level - stop) <= 0:
        return None                                   # never loosen; equal = nothing to do
    if sign * (last - level) <= 0:
        return RatchetAction("exit", level)
    return RatchetAction("tighten", level)
