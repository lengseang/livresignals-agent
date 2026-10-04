"""Risk engine v2 (Contract G): deterministic, no model, no LLM, no clock, no I/O.

It WRAPS the existing risk layer; it never replaces or relaxes it:
  * limits come from `trading.risk.guards.GUARDS` (read-only, validated with `validate_guards`);
  * sizing is `trading.risk.risk_engine.compute_position` (2% risk/trade, 5% max stop, exposure cap);
  * halts are `trading.risk.breakers.check_portfolio_breakers` / `check_strategy_breakers`.
V2 only ADDS checks the old layer lacks (spread, duplicate order, exchange health, kill switch, short
policy, symbol match) and translates everything into Contract G: EVERY check appears, passed or not.

Fail closed: unknown exchange health, unknown kill-switch state, weakened guards, or any internal
error reject. Position size exists only here, as a base-asset quantity (notional / reference price).

Which checks apply to reduce-only intents (exit/reduce/flatten): risk-reducing orders are NOT blocked by
entry limits (daily loss, exposure, spread). Kill switch, exchange health and duplicate-order still apply:
the kill switch blocks every new order by definition ("BLOCK NEW ORDERS"). Listed as "n/a" in the audit log.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from trading.risk.breakers import EquitySnapshot, check_portfolio_breakers, check_strategy_breakers
from trading.risk.guards import GUARDS, validate_guards
from trading.risk.risk_engine import RiskRejected, compute_position
from trading.v2.contracts import REQUIRED_RISK_CHECKS, DecisionState, RiskDecision, TradeProposal
from trading.v2.ids import derive_id

SIZE_DECIMALS = 8


def _floor(x: float) -> float:
    """Round a quantity DOWN to SIZE_DECIMALS: rounding up could overshoot the exposure cap or the
    position held, by a fraction of a cent, which is still a breach."""
    return math.floor(x * 10 ** SIZE_DECIMALS) / 10 ** SIZE_DECIMALS


@dataclass(frozen=True)
class RiskLimits:
    """Limits V2 adds on top of GUARDS. `daily_loss_cap_usd` has no default on purpose: the live
    account's cap (-$1.00 today) is an owner decision; None means "explicitly no absolute cap"."""
    daily_loss_cap_usd: Optional[float]
    max_spread_bps: float = 5.0          # the value in contracts.md Contract G's example
    allow_short: bool = False            # GUARDS: spot only, no margin
    reduce_fraction: float = 0.5         # share of the position a "reduce" intent closes
    # Fraction of notional held back from free cash for the entry fee and slippage, so an approved order
    # can actually be funded. 0.002 = 2x the repo CostModel's 0.10% fee. UNVALIDATED: measure real costs.
    cost_buffer_frac: float = 0.002
    risk_version: str = "risk-v2.0"


@dataclass(frozen=True)
class OpenOrder:
    symbol: str
    position_side: str   # LONG | SHORT
    reduce_only: bool


@dataclass(frozen=True)
class ExchangeHealth:
    ok: bool
    detail: str


@dataclass(frozen=True)
class RiskContext:
    """Facts the engine needs and the caller must supply. Unknowns are None and FAIL CLOSED."""
    state: DecisionState
    day_start_equity: float
    week_start_equity: float
    high_water_mark: float
    open_notional: float
    open_orders: tuple[OpenOrder, ...]
    exchange: Optional[ExchangeHealth]
    kill_switch_engaged: Optional[bool]
    strategy_loss_pct: float
    consecutive_losses: int
    daily_loss_budget_used: float
    leverage: float = 1.0


def _chk(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def evaluate_risk(proposal: TradeProposal, ctx: RiskContext, score_pct: Optional[float],
                  limits: RiskLimits) -> RiskDecision:
    """Always returns a valid RiskDecision. `score_pct` is the policy's adjusted confidence x 100
    (the legacy engine only checks it for None = missing data)."""
    try:
        return _evaluate(proposal, ctx, score_pct, limits)
    except Exception as exc:  # fail closed on ANY internal error
        return _engine_error(proposal, limits, type(exc).__name__)


def _engine_error(proposal: TradeProposal, limits: RiskLimits, cls: str) -> RiskDecision:
    checks = [_chk(n, False, "not evaluated: engine error") for n in REQUIRED_RISK_CHECKS]
    checks.append(_chk("engine_error", False, f"internal error {cls}; failing closed"))
    return RiskDecision(
        contract="risk_decision", version="1.0",
        risk_decision_id=derive_id("rd", proposal.proposal_id, limits.risk_version),
        risk_version=limits.risk_version, proposal_id=proposal.proposal_id, approved=False,
        checks=checks, size=None, stop_price=None, reject_reason="engine_error")


def _evaluate(p: TradeProposal, ctx: RiskContext, score_pct: Optional[float],
              limits: RiskLimits) -> RiskDecision:
    s = ctx.state
    entry = p.intent == "entry"
    eq = s.risk.equity
    c: dict[str, dict] = {}   # name -> check, insertion order = audit order

    def add(name: str, passed: bool, detail: str) -> None:
        c[name] = _chk(name, passed, detail)

    # --- precondition: the legacy guards must be intact, and the proposal must match the state
    problems = validate_guards(GUARDS)
    add("guards_intact", not problems, "intact" if not problems else "; ".join(problems))
    add("proposal_matches_state", p.symbol == s.symbol,
        f"{p.symbol} vs state {s.symbol}" if p.symbol != s.symbol else f"{p.symbol}")

    # --- daily_loss_limit: legacy pct halt (via breakers) AND the optional absolute cap
    now = datetime.fromtimestamp(s.timestamp, tz=timezone.utc)
    snap = EquitySnapshot(utc=now, equity=eq, realized_pnl=0.0, unrealized_pnl=0.0, fees=0.0)
    day = check_portfolio_breakers(ctx.day_start_equity, eq, eq, snap)      # only the daily term can trip
    wk = check_portfolio_breakers(eq, ctx.week_start_equity, eq, snap)      # only the weekly term
    dd = check_portfolio_breakers(eq, eq, ctx.high_water_mark, snap)        # only the drawdown term
    cap = limits.daily_loss_cap_usd
    cap_hit = cap is not None and s.risk.daily_pnl <= -abs(cap)
    daily_detail = (f"daily_pnl {s.risk.daily_pnl:+.2f} vs cap "
                    f"{('-%.2f' % abs(cap)) if cap is not None else 'none'}; "
                    + (day.reason if day.halted else "pct halt clear"))
    if entry:
        add("daily_loss_limit", not (day.halted or cap_hit), daily_detail)
    else:
        add("daily_loss_limit", True, "n/a: reduce-only is never blocked by loss limits; " + daily_detail)

    # --- sizing (entries) / closing size (exits)
    size = stop_price = None
    sizing_detail, sizing_ok = "", True
    stop_ok = True
    if entry:
        stop_ok = p.stop_pct <= GUARDS.max_stop_distance
        short_ok = p.side == "LONG" or limits.allow_short
        add("stop_distance", stop_ok, f"{p.stop_pct:.4f} vs max {GUARDS.max_stop_distance:.4f}")
        add("short_allowed", short_ok, "long" if p.side == "LONG" else
            ("shorting allowed" if limits.allow_short else "shorting not allowed (spot only)"))
        if stop_ok and short_ok:
            try:
                pos = compute_position(score=score_pct, risk_equity=eq, entry=p.reference_price,
                                       stop_distance=p.stop_pct, open_notional=ctx.open_notional,
                                       modifiers=None, macro_multiplier=1.0)
                free = eq - ctx.open_notional
                notional = min(pos["position_notional"], free / (1.0 + limits.cost_buffer_frac))
                capped = notional < pos["position_notional"]
                add("buying_power", notional > 0,
                    (f"capped by free cash: ${notional:.2f} (free ${free:.2f}, cost buffer "
                     f"{limits.cost_buffer_frac:.2%})") if capped else f"free cash ${free:.2f} funds the order")
                qty = _floor(notional / p.reference_price)
                if qty <= 0:
                    sizing_ok, sizing_detail = False, "size rounds to zero"
                else:
                    size = qty
                    stop_price = round(p.reference_price * (1 - p.stop_pct if p.side == "LONG"
                                                              else 1 + p.stop_pct), SIZE_DECIMALS)
                    sizing_detail = (f"notional ${notional:.2f} = "
                                     f"{notional / eq:.2%} of equity; risk "
                                     f"${pos['risk_amount']:.2f} ({pos['risk_fraction']:.4f}); "
                                     f"exposure after {(ctx.open_notional + notional) / eq:.2%}")
                    add("exposure_cap", ctx.open_notional + notional <= GUARDS.max_exposure_fraction * eq + 1e-9,
                        f"{(ctx.open_notional + notional) / eq:.2%} vs cap {GUARDS.max_exposure_fraction:.0%}")
            except RiskRejected as exc:
                sizing_ok, sizing_detail = False, f"legacy engine: {exc}"
        else:
            sizing_ok, sizing_detail = False, "not sized: a precondition failed"
        add("max_position_size", sizing_ok, sizing_detail)
    else:
        held = s.position.size if s.position.side != "none" else 0.0
        frac = limits.reduce_fraction if p.intent == "reduce" else 1.0
        qty = _floor(held * frac)
        have = s.position.side != "none" and s.position.side.upper() == p.side
        sizing_ok = have and qty > 0
        if sizing_ok:
            size = qty
        add("max_position_size", sizing_ok,
            f"n/a: reduce-only, closing {frac:.0%} of {held}" if sizing_ok
            else f"no {p.side} position to {p.intent} (state: {s.position.side})")

    # --- leverage
    limit_lev = float("inf") if GUARDS.allow_leverage else 1.0
    add("max_leverage", ctx.leverage <= limit_lev, f"{ctx.leverage:g}x vs max {limit_lev:g}x")

    # --- spread (entries only)
    if entry:
        add("spread_ceiling", s.market.spread_bps <= limits.max_spread_bps,
            f"{s.market.spread_bps:.2f} bps vs {limits.max_spread_bps:.2f} cap")
    else:
        add("spread_ceiling", True, f"n/a: exits are not blocked by spread ({s.market.spread_bps:.2f} bps)")

    # --- duplicate order / duplicate position
    pos_side = p.side
    dup = [o for o in ctx.open_orders if o.symbol == p.symbol and o.position_side == pos_side
           and o.reduce_only == (not entry)]
    already = entry and s.position.side == p.side.lower()
    add("duplicate_order", not dup and not already,
        "no matching open order" if not dup and not already else
        (f"{len(dup)} matching open order(s)" if dup else f"already {p.side} on {p.symbol}"))

    # --- exchange health / kill switch: unknown fails closed
    h = ctx.exchange
    add("exchange_health", h is not None and h.ok,
        "unknown (failing closed)" if h is None else h.detail)
    ks = ctx.kill_switch_engaged
    add("kill_switch", ks is False,
        "state unknown (failing closed)" if ks is None else ("ENGAGED" if ks else "not engaged"))

    # --- legacy halts as extra checks (entries only)
    for name, st in (("weekly_loss_halt", wk), ("drawdown_halt", dd)):
        if entry:
            add(name, not st.halted, st.reason if st.halted else "clear")
        else:
            add(name, True, "n/a: reduce-only" + ("; would halt: " + st.reason if st.halted else ""))
    sb = check_strategy_breakers(strategy_loss_pct=ctx.strategy_loss_pct,
                                 consecutive_losses=ctx.consecutive_losses,
                                 daily_loss_budget_used=ctx.daily_loss_budget_used)
    add("strategy_breaker", (not sb.halted) or not entry,
        sb.reason if sb.halted else "clear")

    # --- assemble in audit order: the seven spec checks first, then the extras
    ordered = [c[n] for n in REQUIRED_RISK_CHECKS] + [v for k, v in c.items() if k not in REQUIRED_RISK_CHECKS]
    failed = [x["name"] for x in ordered if not x["passed"]]
    approved = not failed and size is not None
    if not approved and not failed:   # size missing without a failed check: cannot happen, but never approve
        ordered.append(_chk("sizing_missing", False, "no size computed"))
        failed = ["sizing_missing"]
    return RiskDecision(
        contract="risk_decision", version="1.0",
        risk_decision_id=derive_id("rd", p.proposal_id, limits.risk_version),
        risk_version=limits.risk_version, proposal_id=p.proposal_id, approved=approved,
        checks=ordered, size=size if approved else None,
        stop_price=stop_price if approved and entry else None,
        reject_reason=None if approved else failed[0])
