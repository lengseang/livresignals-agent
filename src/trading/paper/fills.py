"""The fill model (docs/loop/designs/T4.1.md). PAPER ONLY: decides whether and at what price a simulated
order fills. It never talks to an exchange and imports no exchange client.

Two data modes:

* tick mode (live paper, or recorded bookTicker/aggTrade ticks): one clock, the RECEIVE clock
  (`Tick.ts_recv_ns`). Taker orders fill at the opposite best of the latest book at or before
  `decision + latency`; size beyond L1 is cancelled and flagged (or, as a labelled pessimistic scenario,
  charged at the p99 spread). Resting limits fill only when a real trade prints strictly THROUGH the limit.
  Non-fills are recorded; maker fills carry +1/+10/+60 s markouts (adverse selection, measured).
* kline mode (replay of OHLCV bars, no book): next bar open +/- half the measured spread of the symbol;
  limits fill only if the bar trades strictly through; a bar hitting both stop and take-profit books the stop.

Facts vs assumptions: the fee default (0.1 %/side, VIP 0) is a secondary-source default, the account's real
rate is UNKNOWN. Order latency is UNKNOWN; 0.38 s reuses the measured one-way FEED latency as a scenario.
"""
from __future__ import annotations

import bisect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, TypeVar

from trading.paper import spreads

SEC_NS = 1_000_000_000
LATENCY_SWEEP_S = (0.0, 0.38, 0.75, 1.5)
MARKOUT_HORIZONS_S = (1, 10, 60)
MODE = "PAPER"
FEE_STATUS = ("0.10%/side is the VIP 0 default from secondary sources; this account's real rate is UNKNOWN "
              "(E1 / data/fee-audit.json)")

_T = TypeVar("_T")


@dataclass(frozen=True)
class FillConfig:
    fee_taker: float = 0.001
    fee_maker: float = 0.001
    latency_order_s: float = 0.38          # scenario: the measured one-way FEED latency reused for orders
    maker_rule: str = "trade_through"      # | "touch" (OPTIMISTIC sensitivity only)
    beyond_l1: str = "cancel_rest"         # | "penalty" (pessimistic SCENARIO: remainder at p99 spread)
    maker_timeout_s: float | None = None   # per strategy; a resting order is cancelled after this
    spread_stat: str = "median"            # kline mode: "median" | "p90" (stress) | "p99" | "none" (gross)
    fill_cap_usd: float | None = 5.0       # kline mode: bars have no depth; None = uncapped, labelled
    over_cap: str = "clip"                 # kline mode: "clip" to the cap | "label" (fill, flag depth_unmodelled)
    spread_override: Mapping[str, float] | None = field(default=None, hash=False)

    def __post_init__(self):
        checks = {"maker_rule": ("trade_through", "touch"), "beyond_l1": ("cancel_rest", "penalty"),
                  "spread_stat": ("median", "p90", "p99", "none"), "over_cap": ("clip", "label")}
        for k, ok in checks.items():
            if getattr(self, k) not in ok:
                raise ValueError(f"{k} must be one of {ok}, got {getattr(self, k)!r}")
        if self.latency_order_s < 0:
            raise ValueError("latency_order_s must be >= 0")

    @property
    def latency_ns(self) -> int:
        return int(round(self.latency_order_s * SEC_NS))

    def spread_bp(self, symbol: str, stat: str | None = None) -> float:
        stat = stat or self.spread_stat
        if stat == "none":
            return 0.0
        return spreads.spread_bp(symbol, stat, self.spread_override)

    def labels(self) -> list[str]:
        out = ["PAPER (simulated fills; research, not evidence of edge)",
               f"fee taker {self.fee_taker:.4%} / maker {self.fee_maker:.4%} per side: {FEE_STATUS}",
               f"order latency {self.latency_order_s} s: a scenario (order-leg latency UNKNOWN)"]
        if self.maker_rule == "touch":
            out.append("OPTIMISTIC: maker_rule=touch ignores queue position; never use for the gate")
        if self.beyond_l1 == "penalty":
            out.append("SCENARIO (pessimistic): size beyond L1 charged at the p99 spread; not a measured cost")
        if self.spread_stat == "p90":
            out.append("STRESS: p90 spread (design requires results to survive it)")
        elif self.spread_stat == "none":
            out.append("GROSS: spread not charged (spread_stat=none)")
        if self.spread_override:
            out.append(f"spread override (NOT measured) for: {sorted(self.spread_override)}")
        return out

    def header(self) -> dict:
        d = asdict(self)
        d["spread_override"] = dict(self.spread_override) if self.spread_override else None
        d["fee_status"] = FEE_STATUS
        d["spread_table"] = spreads.source()
        d["labels"] = self.labels()
        return d


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str                    # "buy" | "sell"
    kind: str                    # "taker" | "maker"
    status: str                  # filled | partial | cancelled_timeout | cancelled_by_strategy | no_book
    qty_req: float
    qty: float
    price: float | None
    fee: float
    t_decide_ns: int
    t_arrival_ns: int
    t_fill_ns: int | None
    t_end_ns: int
    book_ts_ns: int | None       # the book record the fill (or marketability check) used
    config: dict
    flags: tuple[str, ...] = ()
    limit: float | None = None
    markout: dict | None = None  # maker fills: signed (mid - price)/price at +1/+10/+60 s; None = UNKNOWN
    missed: dict | None = None   # maker non-fills: what a fill at the limit would have shown at t_end

    @property
    def notional(self) -> float:
        return 0.0 if self.price is None else self.qty * self.price

    def to_record(self) -> dict:
        d = asdict(self)
        d.pop("config")
        d["flags"] = list(self.flags)
        d["notional"] = self.notional
        return {"mode": MODE, "paper": True, "type": "paper_fill", "fill_model": self.config, **d}


# --- tapes (one clock: the receive clock) ---------------------------------------------------------

class _Tape:
    kind = ""

    def __init__(self, ticks: Iterable[Any]):
        by: dict[str, list] = {}
        for t in ticks:
            if t.kind == self.kind:
                by.setdefault(t.symbol, []).append(t)
        self._ticks = {s: sorted(v, key=lambda x: x.ts_recv_ns) for s, v in by.items()}
        self._ts = {s: [x.ts_recv_ns for x in v] for s, v in self._ticks.items()}

    def last_ts(self, symbol: str) -> int | None:
        ts = self._ts.get(symbol)
        return ts[-1] if ts else None


class BookTape(_Tape):
    """bookTicker records per symbol. `at(t)` = latest record received at or before t (never after)."""
    kind = "book"

    def at(self, symbol: str, t_ns: int):
        ts = self._ts.get(symbol)
        if not ts:
            return None
        i = bisect.bisect_right(ts, t_ns)
        return self._ticks[symbol][i - 1] if i else None

    def mid(self, symbol: str, t_ns: int) -> float | None:
        b = self.at(symbol, t_ns)
        return None if b is None else (b.data["bid"] + b.data["ask"]) / 2


class TradeTape(_Tape):
    kind = "trade"

    def between(self, symbol: str, t0_ns: int, t1_ns: int) -> list:
        """Trades received strictly after t0 and at or before t1."""
        ts = self._ts.get(symbol)
        if not ts:
            return []
        return self._ticks[symbol][bisect.bisect_right(ts, t0_ns): bisect.bisect_right(ts, t1_ns)]


# --- tick mode --------------------------------------------------------------------------------------

def _check_side(side: str) -> None:
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy or sell, got {side!r}")


def _taker_at_book(b, symbol: str, side: str, qty: float, t_decide: int, arrival: int, cfg: FillConfig,
                   flags: tuple[str, ...] = (), limit: float | None = None) -> Fill:
    d = b.data
    best, l1 = (d["ask"], d["ask_qty"]) if side == "buy" else (d["bid"], d["bid_qty"])
    filled, price, status, fl = min(qty, l1), best, "filled", list(flags)
    if qty > l1:
        fl.append("exceeds_top_level")
        if cfg.beyond_l1 == "penalty":
            p99 = spreads.spread_bp(symbol, "p99", cfg.spread_override)
            worse = best * (1 + p99 / 1e4) if side == "buy" else best * (1 - p99 / 1e4)
            price = (l1 * best + (qty - l1) * worse) / qty
            filled = qty
            fl.append("penalty_p99_scenario")
        else:
            status = "partial"
    return Fill(symbol, side, "taker", status, qty, filled, price, cfg.fee_taker * filled * price, t_decide,
                arrival, arrival, arrival, b.ts_recv_ns, cfg.header(), tuple(fl), limit)


def _no_book(symbol, side, kind, qty, t_decide, arrival, cfg, limit=None) -> Fill:
    return Fill(symbol, side, kind, "no_book", qty, 0.0, None, 0.0, t_decide, arrival, None, arrival, None,
                cfg.header(), ("no_book_at_arrival",), limit)


def taker_fill(books: BookTape, symbol: str, side: str, qty: float, t_decide_ns: int, cfg: FillConfig) -> Fill:
    """A market order decided at t_decide_ns, arriving at t_decide + latency, filled against the latest book
    received at or before the arrival (price moves during latency are charged)."""
    _check_side(side)
    arrival = t_decide_ns + cfg.latency_ns
    b = books.at(symbol, arrival)
    if b is None:
        return _no_book(symbol, side, "taker", qty, t_decide_ns, arrival, cfg)
    return _taker_at_book(b, symbol, side, qty, t_decide_ns, arrival, cfg)


def maker_order(books: BookTape, trades: TradeTape, symbol: str, side: str, limit: float, qty: float,
                t_decide_ns: int, cfg: FillConfig, timeout_s: float | None = None,
                cancel_ns: int | None = None) -> Fill:
    """A resting limit order. Marketable on arrival -> taker. Otherwise it fills at `limit` only when a real
    trade prints strictly through it (trade_through) and ends filled, cancelled_timeout or cancelled_by_strategy."""
    _check_side(side)
    timeout_s = cfg.maker_timeout_s if timeout_s is None else timeout_s
    if timeout_s is None:
        raise ValueError("maker orders need a timeout (maker_timeout_s is set per strategy)")
    arrival = t_decide_ns + cfg.latency_ns
    b = books.at(symbol, arrival)
    if b is None:
        return _no_book(symbol, side, "maker", qty, t_decide_ns, arrival, cfg, limit)
    marketable = limit >= b.data["ask"] if side == "buy" else limit <= b.data["bid"]
    if marketable:
        return _taker_at_book(b, symbol, side, qty, t_decide_ns, arrival, cfg, ("marketable_on_arrival",), limit)

    end, status = arrival + int(round(timeout_s * SEC_NS)), "cancelled_timeout"
    if cancel_ns is not None and cancel_ns < end:
        end, status = cancel_ns, "cancelled_by_strategy"
    touch = cfg.maker_rule == "touch"
    for tr in trades.between(symbol, arrival, end):
        p = tr.data["price"]
        hit = (p <= limit if touch else p < limit) if side == "buy" else (p >= limit if touch else p > limit)
        if hit:
            f = Fill(symbol, side, "maker", "filled", qty, qty, limit, cfg.fee_maker * qty * limit, t_decide_ns,
                     arrival, tr.ts_recv_ns, tr.ts_recv_ns, b.ts_recv_ns, cfg.header(),
                     ("touch_optimistic",) if touch else (), limit)
            return replace(f, markout=markouts(f, books))
    mid_end = books.mid(symbol, end)
    missed = {"mid_at_end": mid_end,
              "would_have_markout": None if mid_end is None else
              (mid_end - limit) / limit * (1 if side == "buy" else -1),
              "note": "what a fill at the limit would have shown at the cancel time (missed-winner cost)"}
    return Fill(symbol, side, "maker", status, qty, 0.0, None, 0.0, t_decide_ns, arrival, None, end,
                b.ts_recv_ns, cfg.header(), (), limit, None, missed)


def markouts(fill: Fill, books: BookTape, horizons_s: Iterable[float] = MARKOUT_HORIZONS_S) -> dict:
    """Signed markout (mid(t_fill + h) - price) / price, positive = in our favour. UNKNOWN (None) when the
    recording ends before t_fill + h: the book after the last record is not known."""
    out: dict[str, float | None] = {}
    last = books.last_ts(fill.symbol)
    sign = 1 if fill.side == "buy" else -1
    for h in horizons_s:
        key = f"{h:g}s"
        if fill.t_fill_ns is None or fill.price is None or last is None:
            out[key] = None
            continue
        t = fill.t_fill_ns + int(round(h * SEC_NS))
        m = books.mid(fill.symbol, t) if last >= t else None
        out[key] = None if m is None else sign * (m - fill.price) / fill.price
    return out


def fill_report(fills: Iterable[Fill]) -> dict:
    """Fill rate next to P&L, as the design requires: counts by status, maker fill rate, mean markouts."""
    fills = list(fills)
    counts: dict[str, int] = {}
    for f in fills:
        counts[f.status] = counts.get(f.status, 0) + 1
    maker = [f for f in fills if f.kind == "maker" or "marketable_on_arrival" in f.flags]
    resting = [f for f in fills if f.kind == "maker"]
    filled = [f for f in resting if f.status == "filled"]
    mean: dict[str, float | None] = {}
    for h in MARKOUT_HORIZONS_S:
        key = f"{h:g}s"
        vals = [f.markout[key] for f in filled if f.markout and f.markout.get(key) is not None]
        mean[key] = sum(vals) / len(vals) if vals else None
    return {"orders": len(fills), "status_counts": counts, "maker_orders": len(resting),
            "maker_filled": len(filled), "marketable_on_arrival": len(maker) - len(resting),
            "fill_rate": (len(filled) / len(resting)) if resting else None,
            "partial": counts.get("partial", 0), "markout_mean": mean,
            "markout_note": "negative mean markout after maker fills = adverse selection (measured)"}


def latency_sweep(run: Callable[[FillConfig], _T], cfg: FillConfig,
                  latencies: Iterable[float] = LATENCY_SWEEP_S) -> dict[float, _T]:
    """Run the same thing under each order latency; a result that flips between 0.38 and 0.75 s is
    latency-fragile."""
    return {lat: run(replace(cfg, latency_order_s=lat)) for lat in latencies}


# --- kline mode (no book) ---------------------------------------------------------------------------

def _half(symbol: str, cfg: FillConfig) -> float:
    return cfg.spread_bp(symbol) / 2 / 1e4


def bar_taker_price(symbol: str, side: str, open_price: float, cfg: FillConfig) -> float:
    """Next-bar-open execution: open +/- half the measured spread (median, or p90 under stress)."""
    _check_side(side)
    h = _half(symbol, cfg)
    return open_price * (1 + h) if side == "buy" else open_price * (1 - h)


def bar_limit_fill(bar, side: str, limit: float, cfg: FillConfig) -> float | None:
    """A resting limit fills in this bar only if the bar trades strictly through it; price = limit."""
    _check_side(side)
    if cfg.maker_rule == "touch":
        hit = bar.low <= limit if side == "buy" else bar.high >= limit
    else:
        hit = bar.low < limit if side == "buy" else bar.high > limit
    return limit if hit else None


def bar_exit(bar, side: str, stop: float, take_profit: float | None, cfg: FillConfig) -> tuple[str, float] | None:
    """Exit of a `long`/`short` position in one bar. Stops are taker orders (gap-through fills at the open,
    minus half the spread); take-profits are limits (trade-through). A bar hitting both books the STOP."""
    if side == "long":
        if bar.low <= stop:
            return "stop", bar_taker_price(bar.symbol, "sell", min(stop, bar.open), cfg)
        if take_profit is not None and bar_limit_fill(bar, "sell", take_profit, cfg) is not None:
            return "take_profit", take_profit
        return None
    if side == "short":
        if bar.high >= stop:
            return "stop", bar_taker_price(bar.symbol, "buy", max(stop, bar.open), cfg)
        if take_profit is not None and bar_limit_fill(bar, "buy", take_profit, cfg) is not None:
            return "take_profit", take_profit
        return None
    raise ValueError(f"side must be long or short, got {side!r}")
