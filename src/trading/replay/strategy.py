"""The strategy interface, identical for live ticks (T3.2) and replay."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from trading.replay.bars import Bar


@dataclass(frozen=True, slots=True)
class Signal:
    strategy: str
    symbol: str
    action: str          # "open" | "close" | "target" (P1c: hold `weight` of equity)
    ts_ms: int           # the clock time the signal was emitted (a bar's end_ms)
    reason: str
    ref_price: float     # the price the strategy saw (not a fill price)
    interval: str
    side: str = "L"          # "L" long; "S" short (PAPER-ONLY simulation: spot cannot short)
    # Optional exits applied by the broker (docs/loop/designs/T3.4.md): fractional distances from the entry
    # fill's reference price, and a time stop. None = not used; existing signals are unchanged.
    sl: float | None = None
    tp: float | None = None
    max_hold_ms: int | None = None
    # P1c (target weights): fraction of account equity to hold in `symbol` after the fill, 0..1. Only for
    # action "target"; the broker trades the difference. None for every open/close signal.
    weight: float | None = None

    def record(self, mode: str, run_id: str | None = None, params: dict | None = None) -> dict:
        from trading.replay.broker import iso
        return {"mode": mode, "type": "strategy_signal", "ts": iso(self.ts_ms), "ts_ms": self.ts_ms,
                "payload": {"id": f"{self.strategy}:{self.symbol}:{self.ts_ms}", "strategy": self.strategy,
                            "params": params or {}, "symbol": self.symbol, "side": self.side,
                            "action": self.action, "reason": self.reason, "ref_price": self.ref_price,
                            "interval": self.interval, "run_id": run_id,
                            "sl": self.sl, "tp": self.tp, "max_hold_ms": self.max_hold_ms,
                            **({"weight": self.weight} if self.weight is not None else {})}}


class Strategy(Protocol):
    name: str
    params: dict
    intervals: tuple[str, ...]

    def on_bar(self, bar: Bar, ctx) -> list[Signal]: ...
