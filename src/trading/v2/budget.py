"""A daily cap on paid Jev calls (rc4). Scaling to 15 coins x 2 lanes multiplies the bill; a bug or a runaway loop must not.

One `JevBudget` is shared by every client of a lane (entry + position workflows). Over the cap the wrapped client raises
`JevBudgetExceeded`, a `JevBalanceExhausted`, so the adapter / advisor answer from the gateway exactly as they do when the
real balance runs out (labelled `balance_exhausted`), never silently and never by crashing. The day is the UTC day; the
counter resets at its start. Pure and injectable (clock), tests never sleep.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from trading.v2.adapter import JevBalanceExhausted


class JevBudgetExceeded(JevBalanceExhausted):
    pass


class JevBudget:
    def __init__(self, daily_cap: int, clock: Callable[[], float] = time.time):
        if not (isinstance(daily_cap, int) and daily_cap > 0):
            raise ValueError("daily_cap must be a positive integer")
        self.daily_cap, self._clock = daily_cap, clock
        self._day, self._used, self.refused_today, self.total_used = "", 0, 0, 0
        self._lock = threading.Lock()

    def _roll(self) -> None:
        day = datetime.fromtimestamp(self._clock(), tz=timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self._used, self.refused_today = day, 0, 0

    def take(self) -> bool:
        """Reserve one call. False (and counted as refused) when today's cap is spent."""
        with self._lock:
            self._roll()
            if self._used >= self.daily_cap:
                self.refused_today += 1
                return False
            self._used += 1
            self.total_used += 1
            return True

    def snapshot(self) -> dict:
        with self._lock:
            self._roll()
            return {"day": self._day, "used": self._used, "cap": self.daily_cap, "refused": self.refused_today}


class BudgetedClient:
    """Wraps any JevClient. Same `evaluate(request, timeout_s)` contract; `usage` passes through."""

    def __init__(self, inner, budget: JevBudget):
        self.inner, self.budget = inner, budget

    @property
    def usage(self) -> Optional[object]:
        return getattr(self.inner, "usage", None)

    def evaluate(self, request: dict, timeout_s: float) -> object:
        if not self.budget.take():
            raise JevBudgetExceeded("daily Jev call budget spent")
        return self.inner.evaluate(request, timeout_s)

    def __repr__(self) -> str:
        return f"BudgetedClient({self.inner!r}, cap={self.budget.daily_cap})"
