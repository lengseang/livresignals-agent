"""Strategy registry: which plugins exist, with the metadata the terminal and the gate need.

A plugin is a factory for an object satisfying trading.replay.strategy.Strategy (name, params, intervals,
on_bar). The SAME object type runs in replay and in the live scanner. Metadata is descriptive only:
`gate_status` stays "PENDING" until the T3.3 gate has actually evaluated the plugin, and `evidence`
points at where the claim lives in the repo ("UNKNOWN" if nowhere). Nothing here is evidence of edge.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from trading.replay.plugins.trend_sma import TrendSMA

GATE_STATES = ("PASSED", "FAILED", "PENDING", "INSUFFICIENT")      # docs/loop/designs/T6.1.md §4.3


@dataclass(frozen=True)
class PluginInfo:
    name: str
    family: str
    factory: Callable[..., object]
    params: dict = field(default_factory=dict)       # defaults; build() may override
    evidence: str = "UNKNOWN"                        # repo path (#anchor) of the design/replay behind it
    gate_status: str = "PENDING"
    version: str = "1"
    description: str = ""


def _check_strategy(obj, name: str) -> None:
    missing = [a for a in ("name", "params", "intervals", "on_bar") if not hasattr(obj, a)]
    if missing or not callable(getattr(obj, "on_bar", None)):
        raise TypeError(f"plugin {name!r} does not build a Strategy (missing {missing or ['on_bar()']})")


class Registry:
    def __init__(self) -> None:
        self._p: dict[str, PluginInfo] = {}

    def register(self, info: PluginInfo) -> PluginInfo:
        if info.name in self._p:
            raise ValueError(f"plugin {info.name!r} is already registered")
        if info.gate_status not in GATE_STATES:
            raise ValueError(f"gate_status must be one of {GATE_STATES}")
        _check_strategy(info.factory(**info.params), info.name)
        self._p[info.name] = info
        return info

    def get(self, name: str) -> PluginInfo:
        if name not in self._p:
            raise KeyError(f"no plugin {name!r}; registered: {sorted(self._p)}")
        return self._p[name]

    def list(self) -> list[PluginInfo]:
        return [self._p[k] for k in sorted(self._p)]

    def build(self, name: str, **overrides):
        info = self.get(name)
        s = info.factory(**{**info.params, **overrides})
        _check_strategy(s, name)
        return s


DEFAULT = Registry()
DEFAULT.register(PluginInfo(
    name="trend_sma", family="trend", factory=TrendSMA,
    params={"sma": 50, "btc_sma": 50, "btc": "BTCUSDT"},
    evidence="docs/loop/designs/T3.0-T3.1.md#9-the-first-plugin-trend-follower-baseline-read-only-re-implementation",
    gate_status="PENDING",
    description="Daily close > SMA(n) and BTC > SMA(m): long; exit on close < SMA(n). Port of the live "
                "trend_follower rule (no hard stop, daily not 4-hourly). Baseline, not evidence of edge."))
