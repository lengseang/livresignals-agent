"""Walk-forward windows and the untouched final holdout (months as 'YYYY-MM', UTC)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

HOLDOUT_MONTHS = 6


class HoldoutError(RuntimeError):
    """A replay reached into the holdout without --open-holdout."""


def add_months(ym: str, k: int) -> str:
    y, m = map(int, ym.split("-"))
    i = y * 12 + (m - 1) + k
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def ym_ms(ym: str) -> int:
    y, m = map(int, ym.split("-"))
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)


def holdout_start(last_complete_ym: str, months: int = HOLDOUT_MONTHS) -> str:
    """First month of the holdout: the last `months` complete months of data."""
    return add_months(last_complete_ym, -(months - 1))


def check_window(start_ms: int, end_ms: int, holdout_ym: str, open_holdout: bool) -> None:
    if end_ms > ym_ms(holdout_ym) and not open_holdout:
        raise HoldoutError(f"window ends after the holdout start {holdout_ym}; the holdout is for the final "
                           f"gate only (pass --open-holdout knowingly, and it is logged as a trial)")


@dataclass(frozen=True)
class WFWindow:
    fit_start: str
    trade_start: str
    trade_end: str


def walk_forward(start: str, end: str, fit_months: int, trade_months: int) -> list[WFWindow]:
    """Fit on [t - fit, t), trade [t, t + trade), roll by trade, while the trade window fits before end."""
    out, fit0 = [], start
    while True:
        t0 = add_months(fit0, fit_months)
        t1 = add_months(t0, trade_months)
        if ym_ms(t1) > ym_ms(end):
            return out
        out.append(WFWindow(fit0, t0, t1))
        fit0 = add_months(fit0, trade_months)
