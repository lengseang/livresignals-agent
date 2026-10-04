"""The System One gateway: a deterministic decision engine that answers Jev's questions when Jev is down
(docs/loop/designs/V2-15 section 3). No network, no model server, no clock: features in, a Contract D Decision out.

Same principles as Jev, by construction:
  * the SAME typed questions (the nine of `crypto_trading_v2`), answered with probabilities, from the SAME
    features-v1 snapshot plus the position;
  * the answer travels the same road: a Decision carrying probabilities, signals, risk_level and questions, so the
    policy's demotions apply to it exactly as to Jev; it is labelled source="fallback",
    fallback_strategy="system_one_gateway", and its confidence stays below EXECUTE (an entry from it escalates);
  * "choose NO_TRADE or HOLD unless the evidence is clear" (Jev's own action instruction) is the default here too.

What is FITTED vs what is a FORMULA (be honest about which is which):
  * P(long hits +7% before -3.5% within 14d) and the short mirror: L2 logistic regression fitted on 2023-2024 and
    tested once on 2025-2026 (`gateway_model.json`, `docs/v2/evidence/gateway_study.json`). Out of sample it has
    NO skill (long AUC 0.49, Brier skill -0.003): the features-v1 numbers do not predict that outcome. The probability
    used is therefore shrunk toward the TRAIN base rate by the measured out-of-sample Gini (2 AUC - 1, floored at 0):
    0 for the long model, so it says the base rate, which sits AT the break-even probability, and the gateway does not
    want to enter. That is the evidence speaking, not timidity, and it is why `admitted_for_entries` is false. A later
    model that shows skill out of sample earns its own deviation from the base rate automatically.
  * Uncertainty on that probability (SIGMA_P) turns it into P(expected value > 0) = Phi((p - break_even) / SIGMA_P).
  * Every other answer (regime, trend/breakout/reversal/timing/entry quality, risk level) is a transparent formula
    over the features, scaled by TRAIN-period quantiles from the same file, never by invented constants. They are
    descriptive heuristics, not calibrated probabilities.
  * In a position: target_reachable is the driftless barrier probability (distance to stop / distance between stop
    and target): with no measured predictive skill, a driftless walk IS the honest prior. The gateway's position
    action defaults to HOLD (the L0 bracket and the L1 ratchet do the managing; the study found that exiting early
    at half the target COST expectancy); it says EXIT only when the market is disorderly (risk level EXTREME).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from trading.v2.contracts import (
    GATEWAY_STRATEGY, Decision, DecisionState, FeatureSnapshot,
)
from trading.v2.fallback import FALLBACK_MAX_CONFIDENCE
from trading.v2.ids import derive_id

MODEL_FILE = Path(__file__).with_name("gateway_model.json")
SIGMA_P = 0.05                 # uncertainty of the fitted probability (order of the test calibration error)
ACT_AT = 0.55                  # a directional action is chosen only above this (the policy's WATCH floor)
REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "BREAKOUT", "HIGH_VOLATILITY", "LOW_LIQUIDITY")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "EXTREME")
POSITION_ACTIONS = ("HOLD", "TIGHTEN_STOP", "REDUCE", "EXIT")


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


@dataclass(frozen=True)
class _Model:
    w: tuple[float, ...]
    mean: tuple[float, ...]
    std: tuple[float, ...]
    base: float = 0.0          # TRAIN base rate
    shrink: float = 1.0        # out-of-sample Gini (2 AUC - 1, floored at 0): 0 = no skill = say the base rate

    def raw(self, x: list[float]) -> float:
        z = self.w[0] + sum(wi * (xi - m) / s for wi, xi, m, s in zip(self.w[1:], x, self.mean, self.std))
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def p(self, x: list[float]) -> float:
        """The probability the gateway USES: the fitted one, pulled toward the base rate by its measured lack of skill.
        A model that did not beat the base rate out of sample is not allowed to sound more certain than it."""
        return self.base + self.shrink * (self.raw(x) - self.base)


class GatewayModel:
    """The fitted numbers. Loaded from gateway_model.json; refuses a file that does not match features-v1."""

    def __init__(self, data: dict):
        if data.get("feature_version") != "features-v1":
            raise ValueError("gateway model was fitted on a different feature version")
        self.version: str = data["model_version"]
        self.columns: tuple[str, ...] = tuple(data["columns"])
        self.break_even: float = float(data["break_even_p"])
        self.admitted: bool = bool(data["admitted_for_entries"])
        self.q: dict = data["train_quantiles"]
        def mk(side):
            d = data["models"][side]
            if not 0.0 <= d["shrink"] <= 1.0:
                raise ValueError("shrink must be in [0, 1]")
            return _Model(tuple(d["w"]), tuple(d["mean"]), tuple(d["std"]), float(d["train_base_rate"]), float(d["shrink"]))
        self.long, self.short = mk("LONG"), mk("SHORT")
        for m in (self.long, self.short):
            if not (len(m.w) - 1 == len(m.mean) == len(m.std) == len(self.columns)):
                raise ValueError("gateway model dimensions do not match its columns")

    @classmethod
    def load(cls, path: Path = MODEL_FILE) -> "GatewayModel":
        return cls(json.loads(Path(path).read_text()))

    def pct(self, name: str, v: float) -> float:
        """Empirical CDF of a TRAIN feature at v (piecewise linear between stored quantiles)."""
        pts = sorted((float(k), x) for k, x in self.q[name].items())
        if v <= pts[0][1]:
            return pts[0][0] / 2                       # below the lowest stored quantile: coarse, monotone
        for (q0, x0), (q1, x1) in zip(pts, pts[1:]):
            if v <= x1:
                return q0 + (q1 - q0) * ((v - x0) / (x1 - x0) if x1 > x0 else 1.0)
        return (1 + pts[-1][0]) / 2                    # above the highest


def feature_row(f: dict) -> list[float]:
    """features-v1 snapshot dict -> the model's input row (same order as studies.history.FEATURE_COLUMNS)."""
    r, t, v, g = f["returns"], f["technical"], f["volume"], f["regime"]
    return [r["1m"], r["5m"], r["15m"], r["1h"], r["4h"],
            t["rsi"], t["atr"], t["ema_fast_slope"], t["ema_slow_slope"], t["distance_vwap"], v["volume_zscore"],
            1.0 if g["trend"] == "bullish" else 0.0, 1.0 if g["trend"] == "bearish" else 0.0,
            1.0 if g["volatility"] == "high" else 0.0]


@dataclass(frozen=True)
class PositionView:
    """What the gateway (and Jev's position workflow) know about an open position."""
    side: str                 # LONG | SHORT
    entry: float
    stop: Optional[float]
    target: Optional[float]
    best: Optional[float]
    last: float
    minutes_held: int

    def r_unit(self) -> Optional[float]:
        return abs(self.entry - self.stop) if self.stop is not None and self.stop != self.entry else None

    def as_features(self) -> dict:
        s = 1.0 if self.side == "LONG" else -1.0
        r = self.r_unit()
        def R(x):
            return None if r is None or x is None else round(s * (x - self.entry) / r, 4)
        return {"side": self.side, "entry": self.entry, "stop": self.stop, "target": self.target, "last": self.last,
                "unrealized_R": R(self.last), "mfe_R": R(self.best), "stop_R": R(self.stop),
                "target_R": R(self.target), "minutes_held": self.minutes_held}


class Gateway:
    def __init__(self, model: Optional[GatewayModel] = None, *, max_spread_bps: float = 5.0):
        self.model = model or GatewayModel.load()
        self.max_spread_bps = max_spread_bps

    # ------------------------------------------------------------ the nine entry questions

    def answers(self, f: dict, spread_bps: float) -> dict:
        m, r, t, v, g = self.model, f["returns"], f["technical"], f["volume"], f["regime"]
        x = feature_row(f)
        p_long, p_short = m.long.p(x), m.short.p(x)
        ev_long = _phi((p_long - m.break_even) / SIGMA_P)
        ev_short = _phi((p_short - m.break_even) / SIGMA_P)

        signs = [_sign(r["15m"]), _sign(r["1h"]), _sign(r["4h"]), _sign(t["ema_fast_slope"])]
        agree = abs(sum(signs)) / 4.0
        direction = _sign(sum(signs))
        trend_quality = _clip(agree * m.pct("abs_ema_fast_slope", abs(t["ema_fast_slope"])))
        breakout = _clip(0.5 * m.pct("volume_zscore", v["volume_zscore"]) + 0.5 * m.pct("abs_ret_15m", abs(r["15m"])))
        reversal = _clip(0.7 * _clip((abs(t["rsi"] - 50.0) - 20.0) / 20.0)
                         + 0.3 * m.pct("abs_distance_vwap", abs(t["distance_vwap"])))
        timing = _clip(1.0 - m.pct("abs_ret_15m", abs(r["15m"])))
        spread_room = _clip(1.0 - spread_bps / self.max_spread_bps)
        entry_quality = _clip((timing + spread_room + (1.0 - m.pct("abs_distance_vwap", abs(t["distance_vwap"])))) / 3.0)

        atr_pct = m.pct("atr", t["atr"])
        if g["liquidity"] == "thin" or spread_bps > 2 * self.max_spread_bps or atr_pct >= 0.99:
            risk = "EXTREME"
        elif atr_pct >= 0.90 or g["volatility"] == "high":
            risk = "HIGH"
        elif atr_pct < 0.25 and spread_bps <= self.max_spread_bps / 2:
            risk = "LOW"
        else:
            risk = "MEDIUM"

        if g["liquidity"] == "thin":
            regime = "LOW_LIQUIDITY"
        elif breakout >= 0.9 and agree >= 0.75:
            regime = "BREAKOUT"
        elif g["volatility"] == "high" and g["trend"] == "neutral":
            regime = "HIGH_VOLATILITY"
        elif g["trend"] == "bullish" and r["1h"] > 0 and r["4h"] > 0:
            regime = "TREND_UP"
        elif g["trend"] == "bearish" and r["1h"] < 0 and r["4h"] < 0:
            regime = "TREND_DOWN"
        else:
            regime = "RANGE"
        return {"p_long": p_long, "p_short": p_short, "ev_long": ev_long, "ev_short": ev_short,
                "trend_quality": trend_quality, "breakout_probability": breakout, "reversal_probability": reversal,
                "timing_quality": timing, "entry_quality": entry_quality, "risk_level": risk, "market_regime": regime,
                "direction": direction}

    # ------------------------------------------------------------ position questions

    def position_answers(self, f: dict, spread_bps: float, pos: PositionView) -> dict:
        a = self.answers(f, spread_bps)
        s = 1 if pos.side == "LONG" else -1
        r, t = f["returns"], f["technical"]
        against = [x for x in (r["15m"], r["1h"]) if _sign(x) == -s]
        momentum_fading = _clip(0.5 * len(against) / 2 + 0.5 * (1 - a["trend_quality"] if a["direction"] != s else 0.0))
        reversal_risk = _clip(a["reversal_probability"] if _sign(t["rsi"] - 50) == s else a["reversal_probability"] / 2)
        if pos.stop is not None and pos.target is not None and s * (pos.target - pos.stop) > 0:
            reach = _clip(s * (pos.last - pos.stop) / (s * (pos.target - pos.stop)))
        else:
            reach = 0.5
        if a["risk_level"] == "EXTREME":
            # a RISK rule, not a prediction: in a disorderly market (top 1% ATR, thin book, or a very wide spread) get out.
            # 0.72 clears the risk-reducing EXIT threshold (0.70); it is the only case where the gateway exits.
            dist = {"HOLD": 0.1, "TIGHTEN_STOP": 0.08, "REDUCE": 0.1, "EXIT": 0.72}
        else:
            dist = {"HOLD": 0.7, "TIGHTEN_STOP": 0.2, "REDUCE": 0.05, "EXIT": 0.05}
        return {**a, "momentum_fading": momentum_fading, "reversal_risk": reversal_risk, "target_reachable": reach,
                "position_action_distribution": dist,
                "position_action": max(dist, key=dist.get)}

    # ------------------------------------------------------------ Contract D

    def decide(self, state: DecisionState, features: FeatureSnapshot, *, workflow_tag: str, reason: str,
               latency_ms: float = 0.0, position: Optional[PositionView] = None) -> Decision:
        if features.snapshot_id != state.features:
            raise ValueError("features do not belong to this state")
        if features.feature_version not in ("features-v1", "features-v2"):     # v2 = v1 + context; the model reads v1
            raise ValueError(f"gateway is fitted on features-v1, got {features.feature_version}")
        f = features.to_dict()
        spread = state.market.spread_bps
        side = state.position.side
        if side == "none":
            a = self.answers(f, spread)
            ql = a["ev_long"]
            qs = a["ev_short"]
            if a["risk_level"] == "EXTREME":
                ql, qs = ql * 0.1, qs * 0.1
            raw = {"LONG": ql, "SHORT": qs, "NO_TRADE": (1 - ql) * (1 - qs), "HOLD": 0.0, "EXIT": 0.0, "REDUCE": 0.0}
            tot = sum(raw.values())
            probs = {k: v / tot for k, v in raw.items()}
            best = max(("LONG", "SHORT"), key=lambda k: probs[k])
            action = best if probs[best] >= ACT_AT else "NO_TRADE"
            edge = ql if best == "LONG" else qs
        else:
            pv = position or PositionView(side=side.upper(), entry=state.position.entry_price, stop=None,
                                          target=None, best=None, last=state.market.price, minutes_held=0)
            a = self.position_answers(f, spread, pv)
            d = a["position_action_distribution"]
            probs = {"HOLD": d["HOLD"] + d["TIGHTEN_STOP"], "EXIT": d["EXIT"], "REDUCE": d["REDUCE"],
                     "LONG": 0.0, "SHORT": 0.0, "NO_TRADE": 0.0}
            action = max(("HOLD", "EXIT", "REDUCE"), key=lambda k: probs[k])
            edge = a["ev_long"] if side == "long" else a["ev_short"]
        conf = min(probs[action], FALLBACK_MAX_CONFIDENCE)
        rl = a["risk_level"]
        qa = lambda ans, p: {"answer": ans, "probability": round(_clip(p), 6)}  # noqa: E731
        return Decision(
            contract="decision", version="1.0",
            decision_id=derive_id("dec", state.state_hash(), workflow_tag, "gateway"),
            state_id=state.state_id, state_hash=state.state_hash(), source="fallback",
            fallback_reason=reason[:300], fallback_strategy=GATEWAY_STRATEGY,
            model=self.model.version, workflow_version=workflow_tag, action=action, confidence=conf,
            probabilities=probs,
            signals={"trend_quality": a["trend_quality"], "entry_quality": a["entry_quality"],
                     "expected_edge": edge, "timing_quality": a["timing_quality"]},
            risk_level=rl,
            questions={
                "market_regime": qa(a["market_regime"], 0.6),
                "trend_quality": qa(f"{a['trend_quality']:.4f}", a["trend_quality"]),
                "entry_quality": qa(f"{a['entry_quality']:.4f}", a["entry_quality"]),
                "breakout_probability": qa(f"{a['breakout_probability']:.4f}", a["breakout_probability"]),
                "reversal_probability": qa(f"{a['reversal_probability']:.4f}", a["reversal_probability"]),
                "risk_level": qa(rl, 0.7)},
            latency_ms=latency_ms)
