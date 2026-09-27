"""Measured per-symbol spreads (versioned table copied from docs/research/spreads.md). Paper only.

An unmeasured symbol is UNKNOWN: `spread_bp` raises instead of guessing, unless the caller passes an
explicit override (which the paper broker then prints as a label on every result).
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

TABLE_PATH = Path(__file__).parent / "tables" / "spreads_v1.json"
STATS = ("median", "p90", "p99")


class UnknownSpreadError(KeyError):
    """No measured spread exists for this symbol; simulating it would need an invented number."""


@lru_cache(maxsize=1)
def load_table() -> dict:
    return json.loads(TABLE_PATH.read_text(encoding="utf-8"))


def spread_bp(symbol: str, stat: str = "median", override: Mapping[str, float] | None = None) -> float:
    """Full quoted spread in bp for `symbol` at `stat` (median, p90 or p99)."""
    if stat not in STATS:
        raise ValueError(f"stat must be one of {STATS}, got {stat!r}")
    if override and symbol in override:
        return float(override[symbol])
    row = load_table()["symbols"].get(symbol)
    if row is None:
        raise UnknownSpreadError(f"{symbol}: spread UNKNOWN (not in {TABLE_PATH.name}); refusing to simulate. "
                                 "Pass an explicit spread_override to run it anyway (labelled in the result).")
    return float(row[f"{stat}_bp"])


def source() -> dict:
    t = load_table()
    return {"table": TABLE_PATH.name, "version": t["_version"], "source": t["_source"], "window": t["_window"],
            "unit_used": "bp (ticks UNKNOWN: tick size not recorded)"}
