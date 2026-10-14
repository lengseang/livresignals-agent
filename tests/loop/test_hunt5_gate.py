"""hunt5's split-half gate: the guard the standing hunt was missing.

Design: docs/proposals/hunt5-split-half-verdict-2026-10-13.md, skill
strategy-generation-loop §"a regime-split gate is NOT a split-half gate".

Why this exists: hunt5 originally gated findings only on the DOWN windows plus
an absolute up-net floor, and recorded 65 of ~240 configs (27%) — vs the ~1% the
strategy-book gate yields. `split_half()` adds rule 2 (net>0 AND PF>1.0 in BOTH
independent halves); a one-half candidate must be rejected, not recorded.

Everything here is a synthetic fixture — REPLAY, not real data.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import hunt5_open_ended as H  # noqa: E402

STEP = 4 * 60 * 60 * 1000          # 4h in ms
NBARS = 200                        # whole fixture = 100 IS + 100 OOS bars


def _ms(s: str) -> int:
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


# The fixture's OWN period, monkeypatched over hunt5's real 2022..2026 constants
# so split_half() splits and measures coverage against the fixture, not the DB.
START = "2022-01-01"
END = dt.datetime.fromtimestamp((_ms(START) + NBARS * STEP) / 1000, dt.timezone.utc).isoformat()


def _db(closes: list[float]) -> sqlite3.Connection:
    """An in-memory candles table: one contiguous 4h series for EVERY hunt5 symbol.

    `run()`/`split_half()` are hard-wired to `SYMS` with min_coverage=0.8, so the
    fixture must cover all of them (import the list, never duplicate it) and be
    contiguous from START, or the coverage guard refuses the window.
    open=high=low=close so the fill is exactly `close`.
    """
    con = sqlite3.connect(":memory:")
    con.execute("create table candles (symbol text, timeframe text, open_time_utc "
                "integer, open real, high real, low real, close real, volume real)")
    t0 = _ms(START)
    for sym in H.SYMS:
        for i, c in enumerate(closes):
            t = t0 + i * STEP
            con.execute("insert into candles values (?,?,?,?,?,?,?,?)",
                        (sym, "4h", t, c, c, c, c, 1.0))
    return con


def _down_then(direction: float) -> list[float]:
    """100 bars drifting down (a short wins), then 100 bars drifting `direction`.

    direction=-2 keeps falling (short wins in BOTH halves); +2 reverses up
    (short loses the second half). Prices engineered, not market data.
    """
    return [300.0 - i for i in range(100)] + [200.0 + direction * i for i in range(100)]


def _always(bars) -> bool:
    """Enter on every bar — the outcome is a pure function of the price path."""
    return True


@pytest.fixture(autouse=True)
def _fixture_period(monkeypatch):
    """Point hunt5's period at the fixture so split_half() works on it."""
    monkeypatch.setattr(H, "START", START)
    monkeypatch.setattr(H, "END", END)


def test_split_half_passes_when_both_halves_positive():
    # Both halves fall; stop/target 50% survives to the hold exit => short nets in each.
    ok, isr, oos = H.split_half(_db(_down_then(-2.0)), _always, "short", 0.5, 0.5, 10,
                                _ms(START) + 100 * STEP)
    assert ok is True, f"expected PASS, got IS={isr} OOS={oos}"
    assert isr["net"] > 0 and isr["pf"] > 1.0
    assert oos["net"] > 0 and oos["pf"] > 1.0


def test_split_half_rejects_when_second_half_negative():
    # First half falls (short wins); second half RISES (short loses) => reject.
    ok, _, _ = H.split_half(_db(_down_then(+2.0)), _always, "short", 0.5, 0.5, 10,
                            _ms(START) + 100 * STEP)
    assert ok is False, "a one-half candidate must be rejected"


def test_split_half_rejects_on_insufficient_sample():
    # Ten bars total => each half under the 30-trade floor => False, whatever the path.
    closes = [300.0 - i for i in range(10)]
    ok, _, _ = H.split_half(_db(closes), _always, "short", 0.5, 0.5, 10,
                            _ms(START) + 5 * STEP)
    assert ok is False


def test_split_half_is_deterministic():
    c = _down_then(-2.0)
    a = H.split_half(_db(c), _always, "short", 0.5, 0.5, 10, _ms(START) + 100 * STEP)
    b = H.split_half(_db(c), _always, "short", 0.5, 0.5, 10, _ms(START) + 100 * STEP)
    assert (a[0], a[1]["n"], a[1]["net"], a[2]["net"]) == (b[0], b[1]["n"], b[1]["net"], b[2]["net"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
