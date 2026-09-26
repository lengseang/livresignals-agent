"""KlineBroker: the replay engine's account, priced by the measured fill model (kline mode). PAPER ONLY.

Plugs into trading.replay.engine.replay() in place of the legacy PaperAccount (same Broker interface), so
strategy plugins are unchanged. Differences from the legacy flat cost model:

* each side pays half the MEASURED spread of that symbol (median, or p90 as the stress run), not one
  assumed number for every symbol; an unmeasured symbol is refused (UNKNOWN) unless overridden;
* a per-fill notional cap (default $5), because bars carry no depth: entries above it are clipped, or,
  with over_cap="label" (or no cap), filled and flagged `depth_unmodelled`;
* every fill record carries paper=True and the fill-model parameters it was produced with.
"""
from __future__ import annotations

from collections.abc import Iterable

from trading.paper.fills import FillConfig, bar_exit, bar_taker_price
from trading.replay.broker import CostModel, PaperAccount


class KlineBroker(PaperAccount):
    def __init__(self, equity: float, slots: int, cfg: FillConfig, name: str = "",
                 symbols: Iterable[str] | None = None):
        # replay orders are market orders at the next open: the taker fee applies on both sides
        super().__init__(equity, slots, CostModel(fee_rate=cfg.fee_taker), name=name)
        self.cfg = cfg
        self._header = cfg.header()
        self.seen_flags: set[str] = set()
        for s in symbols or ():
            cfg.spread_bp(s)                  # raises UnknownSpreadError before any replay work

    # --- pricing hooks ------------------------------------------------------------------------
    def buy_price(self, symbol: str, mid: float) -> float:
        return bar_taker_price(symbol, "buy", mid, self.cfg)

    def sell_price(self, symbol: str, mid: float) -> float:
        return bar_taker_price(symbol, "sell", mid, self.cfg)

    def bar_exit(self, bar, pos: dict):
        """Stop / take-profit through the measured fill model's kline rule (fills.bar_exit)."""
        short = pos.get("side") == "S"
        stop = pos["stop"] if pos["stop"] is not None else (float("inf") if short else float("-inf"))
        return bar_exit(bar, "short" if short else "long", stop, pos["take_profit"], self.cfg)

    def exit_fee_rate(self, reason: str) -> float:
        return self.cfg.fee_maker if reason == "take_profit" else self.cfg.fee_taker    # TP is a resting limit

    def size_budget(self, symbol: str, budget: float) -> tuple[float, list]:
        cap = self.cfg.fill_cap_usd
        if cap is None:
            flags = ["depth_unmodelled"]
        elif budget <= cap:
            flags = []
        elif self.cfg.over_cap == "clip":
            budget, flags = cap, ["capped_at_fill_cap"]
        else:
            flags = ["depth_unmodelled"]
        self.seen_flags.update(flags)
        return budget, flags

    def exit_flags(self, symbol: str, notional: float) -> list:
        cap = self.cfg.fill_cap_usd
        flags = ["depth_unmodelled"] if cap is None or notional > cap else []
        self.seen_flags.update(flags)
        return flags

    def fill_meta(self, flags: list) -> dict:
        return {"paper": True, "flags": list(flags), "fill_model": self._header}

    def labels(self) -> list[str]:
        out = list(self.cfg.labels())
        out.append(f"kline mode: next 1m open +/- half the {self.cfg.spread_stat} spread per symbol "
                   f"(unit used: bp; ticks UNKNOWN); market orders pay the taker fee")
        if self.cfg.fill_cap_usd is None or "depth_unmodelled" in self.seen_flags:
            out.append("depth_unmodelled")
        if "capped_at_fill_cap" in self.seen_flags:
            out.append(f"entries clipped to the ${self.cfg.fill_cap_usd:g} per-fill cap")
        return out
