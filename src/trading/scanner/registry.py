"""Strategy registry: which plugins exist, with the metadata the terminal and the gate need.

A plugin is a factory for an object satisfying trading.replay.strategy.Strategy (name, params, intervals,
on_bar). The SAME object type runs in replay and in the live scanner. Metadata is descriptive only:
`gate_status` stays "PENDING" until the T3.3 gate has actually evaluated the plugin, and `evidence`
points at where the claim lives in the repo ("UNKNOWN" if nowhere). Nothing here is evidence of edge.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from trading.replay.plugins.buy_hold import BuyHoldBTC, BuyHoldEW
from trading.replay.plugins.ctrend import CTrendLite
from trading.replay.plugins.donchian import DonchianEnsemble
from trading.replay.plugins.intraday import IntradayMomentum, IntradayReversal
from trading.replay.plugins.range_breakout import RangeBreakout
from trading.replay.plugins.regime_ls import RegimeLS
from trading.replay.plugins.trend_sma import TrendSMA
from trading.replay.plugins.xs_momentum import XsMomentum
from trading.replay.plugins.tsmom_rm import TsmomRiskManaged

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

_T34 = "docs/loop/designs/T3.4.md"
DEFAULT.register(PluginInfo(
    name="intraday_momentum", family="intraday", factory=IntradayMomentum,
    params={"lookback_h": 4, "z": 2.0, "vol_h": 168, "sl": 0.01, "tp": 0.02, "max_hold_h": 24},
    evidence=f"{_T34}#1-intraday_momentum-and-intraday_reversal-audit-2-wen-et-al-2022-hourly-horizon",
    description="1h close: L-hour return z-score (vs a week of hourly vol) >= +z -> long; SL/TP 2R/time stop. "
                "Audit #2 hypothesis, not evidence of edge."))
DEFAULT.register(PluginInfo(
    name="intraday_reversal", family="intraday", factory=IntradayReversal,
    params={"lookback_h": 4, "z": 2.0, "vol_h": 168, "sl": 0.01, "tp": 0.02, "max_hold_h": 24},
    evidence=f"{_T34}#1-intraday_momentum-and-intraday_reversal-audit-2-wen-et-al-2022-hourly-horizon",
    description="1h close: L-hour return z-score <= -z -> long (buy the fall); SL/TP 2R/time stop. "
                "Audit #2 hypothesis, not evidence of edge."))
DEFAULT.register(PluginInfo(
    name="range_breakout", family="breakout", factory=RangeBreakout,
    params={"interval": "1h", "n": 24, "vol_mult": 2.0, "sl": 0.01, "tp": 0.02, "max_hold_h": 24},
    evidence=f"{_T34}#2-range_breakout-audit-8",
    description="Close above the prior N-bar high with volume > k x average -> long; SL/TP 2R/time stop. "
                "Audit #8 hypothesis, not evidence of edge."))
DEFAULT.register(PluginInfo(
    name="regime_ls", family="regime", factory=RegimeLS,
    params={"sma_regime": 200, "sma_trend": 50, "mom_days": 30, "topn": 3, "max_hold_days": 21},
    evidence=f"{_T34}#3-regime_ls-audit-9-a-port-of-the-rule-documented-in-scriptsregime_traderpy-read-only-not-imported",
    description="BTC > SMA_R: long the strongest coins above SMA50; else SHORT (PAPER-ONLY) the weakest below "
                "SMA50; exits on SMA50 cross / 21-day hold. Audit #9, not evidence of edge."))
DEFAULT.register(PluginInfo(
    name="ctrend_lite", family="cross_section", factory=CTrendLite,
    params={"window_weeks": 52, "quantile": 5, "btc": "BTCUSDT"},
    evidence="research/candidates/candidates.json#A2-ctrend-lite",
    description="Weekly: 28 technical indicators -> 52-week Fama-MacBeth forecasts -> elastic-net selection "
                "(Fieberg et al. 2025 JFQA); hold the top CTREND quintile, long-only. Candidate A2, not evidence "
                "of edge."))
DEFAULT.register(PluginInfo(
    name="donchian_ensemble", family="trend", factory=DonchianEnsemble,
    params={}, evidence="research/candidates/candidates.json#A1-donchian-ensemble",
    description="Daily: 9 Donchian lookbacks (5-360d) per coin with trailing midpoint stops, share of open "
                "lookbacks x 25% vol target (90d), 1/K per eligible coin, target weights (Zarattini et al. 2025). "
                "Candidate A1, not evidence of edge."))
DEFAULT.register(PluginInfo(
    name="tsmom_risk_managed", family="trend", factory=TsmomRiskManaged,
    params={}, evidence="research/candidates/candidates.json#A3-tsmom-risk-managed",
    description="Daily: hold each of 7 majors while its 30-day return > 0, equal weight; exposure x0.5 while "
                "the equal-weight market is >15% off its peak (Howden & Andreev 2026). Candidate A3, not "
                "evidence of edge."))
DEFAULT.register(PluginInfo(
    name="xs_momentum", family="cross_section", factory=XsMomentum,
    params={}, evidence="research/candidates/candidates.json#A6-xs-momentum",
    description="Weekly: hold the top quintile by past 3-week return, equal weight, long-only (Liu, Tsyvinski & "
                "Wu 2022). Candidate A6, control for A2. Not evidence of edge."))
DEFAULT.register(PluginInfo(
    name="buy_hold_btc", family="benchmark", factory=BuyHoldBTC, params={},
    description="Benchmark: BTC at weight 1 from the first daily close, held. Shadow benchmark lane."))
DEFAULT.register(PluginInfo(
    name="buy_hold_ew", family="benchmark", factory=BuyHoldEW, params={},
    description="Benchmark: each universe coin at 1/N from its first daily close, never rebalanced. Shadow "
                "benchmark lane."))
