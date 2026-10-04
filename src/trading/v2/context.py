"""A ContextProvider (for the pipeline) backed by the V2 paper book.

Supplies position, equity, day/week start, high-water mark, open exposure and open orders from the
book. The things a book cannot know are INJECTED and unknown means None, which the risk engine
fails closed on: exchange health, kill switch, exit params, the current system's opinion.
`daily_loss_budget_usd` has no default: how the legacy "2x daily loss budget" breaker defines its
budget is not written down anywhere (escalation E8), so the owner's number is required.
"""
from __future__ import annotations

from typing import Callable, Optional

from trading.risk.guards import GUARDS
from trading.v2.contracts import PositionState, RiskState, StrategyRef
from trading.v2.paper import PaperBook
from trading.v2.policy import ExitParams
from trading.v2.risk import ExchangeHealth, OpenOrder, RiskLimits
from trading.v2.shadow import CycleContext


class PaperContextProvider:
    def __init__(self, book: PaperBook, strategy: StrategyRef, limits: RiskLimits, *,
                 exit_params: Optional[ExitParams],
                 daily_loss_budget_usd: Optional[float],
                 exchange_health: Callable[[], Optional[ExchangeHealth]],
                 kill_switch: Callable[[], Optional[bool]],
                 legacy_action: Optional[Callable[[str, int], Optional[str]]] = None,
                 paused: Callable[[], tuple] = lambda: ()):
        self.book, self.strategy, self.limits = book, strategy, limits
        self.exit_params, self.budget = exit_params, daily_loss_budget_usd
        self._health, self._kill, self._legacy, self._paused = exchange_health, kill_switch, legacy_action, paused

    def context(self, symbol: str, as_of_ms: int) -> CycleContext:
        b = self.book
        eq = b.equity()
        pos = b.positions.get(symbol)
        position = (PositionState(side="none", size=0, entry_price=None, unrealized_pnl=0) if pos is None else
                    PositionState(side=pos.side.lower(), size=pos.qty, entry_price=pos.entry,
                                  unrealized_pnl=((b.last.get(symbol, pos.entry) - pos.entry) * pos.qty
                                                  if pos.side == "LONG" else
                                                  (pos.entry - b.last.get(symbol, pos.entry)) * pos.qty)))
        pnl = b.daily_pnl()
        pct_room = b.day_start_equity * abs(GUARDS.daily_loss_halt) + pnl
        cap_room = (self.limits.daily_loss_cap_usd + pnl) if self.limits.daily_loss_cap_usd is not None else pct_room
        remaining = max(0.0, min(pct_room, cap_room))
        used = (max(0.0, -pnl) / self.budget) if self.budget else 0.0
        return CycleContext(
            position=position,
            risk_state=RiskState(equity=eq, daily_pnl=pnl, daily_loss_limit_remaining=remaining,
                                 current_exposure=b.open_notional() / eq if eq > 0 else 0.0),
            strategy=self.strategy, exit_params=self.exit_params,
            day_start_equity=b.day_start_equity, week_start_equity=b.week_start_equity,
            high_water_mark=b.high_water_mark, open_notional=b.open_notional(),
            open_orders=tuple(OpenOrder(c.symbol, c.position_side, c.reduce_only) for c, _ in b.pending),
            exchange=self._health(), kill_switch_engaged=self._kill(),
            strategy_loss_pct=min(0.0, b.realized_pnl_pct()), consecutive_losses=b.consecutive_losses(),
            daily_loss_budget_used=used, leverage=1.0, paused=tuple(self._paused()),
            legacy_action=self._legacy(symbol, as_of_ms) if self._legacy else None)
