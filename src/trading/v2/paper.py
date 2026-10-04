"""V2 paper book (V2.1): fills approved ExecutionCommands in SIMULATION. There is no order path here.

A separate book from the shadow log and from anything real: its own hash-chain lane, `real: false`,
labelled PAPER. It exists so the pipeline has a position state and an equity curve.

Rules (each has a test):
  * Size and stop come from the command (the risk engine's numbers); the book never sizes anything.
  * An order submitted at time t fills at the OPEN of the first 1m bar that opens at or after t
    (no look-ahead), priced with the repo's `CostModel` (half spread + slippage) and a fee per side.
  * `idempotency_key` makes a repeated submit a no-op: a retry can never become a second position.
  * One position per symbol; a reduce-only order closes at most what is held.
  * The protective stop is checked on every bar after fills, including the entry bar; a gap through the
    stop fills at the open (worse), never at the stop.
  * LIMIT orders are rejected, not approximated. SHORT is a paper-only simulation (spot cannot short;
    no borrow cost or liquidation modelled).
  * Costs default to the replay CostModel (0.10% fee, no spread/slippage): the account's real figures are
    UNKNOWN until measured, so results flatter reality by exactly that gap.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

from trading.replay.bars import Bar
from trading.replay.broker import CostModel
from trading.v2.contracts import ExecutionCommand

log = logging.getLogger("trading.v2.paper")
LABEL = "PAPER V2 (simulated, not real money)"


class Sink(Protocol):
    def write(self, rec: dict) -> dict: ...


@dataclass
class PaperPosition:
    side: str                 # LONG | SHORT
    qty: float
    entry: float              # fill price incl. costs
    stop: Optional[float]
    opened_ms: int
    entry_fee: float          # fee still attributable to the open quantity


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _week(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
    return (d - timedelta(days=d.weekday())).isoformat()      # Monday of the UTC week


class PaperBook:
    def __init__(self, start_equity: float, costs: CostModel = CostModel(), sink: Optional[Sink] = None):
        if not start_equity > 0:
            raise ValueError("start_equity must be > 0")
        self.start_equity = self.cash = float(start_equity)
        self.costs, self.sink = costs, sink
        self.positions: dict[str, PaperPosition] = {}
        self.pending: list[tuple[ExecutionCommand, int]] = []
        self.last: dict[str, float] = {}
        self.trades: list[dict] = []
        self.stats: Counter = Counter()
        self._keys: set[str] = set()
        self._day = self._week = ""
        self.day_start_equity = self.week_start_equity = self.high_water_mark = float(start_equity)

    # ------------------------------------------------------------------ accounting views

    def equity(self) -> float:
        v = self.cash
        for sym, p in self.positions.items():
            px = self.last.get(sym, p.entry)
            v += p.qty * px if p.side == "LONG" else -p.qty * px
        return v

    def open_notional(self) -> float:
        return sum(p.qty * self.last.get(s, p.entry) for s, p in self.positions.items())

    def daily_pnl(self) -> float:
        return self.equity() - self.day_start_equity

    def consecutive_losses(self) -> int:
        n = 0
        for t in reversed(self.trades):
            if t["pnl"] < 0:
                n += 1
            else:
                break
        return n

    def realized_pnl_pct(self) -> float:
        return sum(t["pnl"] for t in self.trades) / self.start_equity

    # ------------------------------------------------------------------ orders

    def submit(self, cmd: ExecutionCommand, ts_ms: int) -> str:
        """Queue a command. Returns 'queued', 'duplicate_ignored' or 'rejected:<why>'."""
        if cmd.idempotency_key in self._keys:
            self.stats["duplicate_ignored"] += 1
            self._emit("duplicate_ignored", ts_ms, cmd.symbol, key=cmd.idempotency_key)
            return "duplicate_ignored"
        self._keys.add(cmd.idempotency_key)
        if cmd.type != "MARKET":
            return self._reject(cmd, ts_ms, "limit_orders_not_supported_in_paper_v1")
        self.pending.append((cmd, ts_ms))
        self.stats["queued"] += 1
        self._emit("queued", ts_ms, cmd.symbol, command=cmd.to_dict())
        return "queued"

    def cancel_pending(self, ts_ms: int, why: str) -> int:
        """Cancel every queued, unfilled command (Gate 6: kill switch cancels orders). Returns how many."""
        n = len(self.pending)
        for cmd, _ in self.pending:
            self.stats[f"cancelled:{why}"] += 1
            self._emit("cancelled", ts_ms, cmd.symbol, reason=why, key=cmd.idempotency_key)
        self.pending = []
        return n

    def _reject(self, cmd: ExecutionCommand, ts_ms: int, why: str) -> str:
        self.stats[f"rejected:{why}"] += 1
        self._emit("rejected", ts_ms, cmd.symbol, reason=why, key=cmd.idempotency_key)
        return f"rejected:{why}"

    # ------------------------------------------------------------------ one closed 1m bar

    def on_bar(self, bar: Bar) -> None:
        if bar.interval != "1m":
            raise ValueError("PaperBook.on_bar takes closed 1m bars")
        self._roll(bar.open_ms)
        keep = []
        for cmd, ts in self.pending:
            if cmd.symbol != bar.symbol or bar.open_ms < ts:
                keep.append((cmd, ts))
                continue
            self._fill(cmd, bar)
        self.pending = keep
        self._check_stop(bar)
        self.last[bar.symbol] = bar.close
        self.high_water_mark = max(self.high_water_mark, self.equity())

    def _roll(self, open_ms: int) -> None:
        d, w = _day(open_ms), _week(open_ms)
        if d != self._day:
            if self._day:
                self.day_start_equity = self.equity()
            self._day = d
        if w != self._week:
            if self._week:
                self.week_start_equity = self.equity()
            self._week = w

    def _fill(self, cmd: ExecutionCommand, bar: Bar) -> None:
        sym, t = cmd.symbol, bar.open_ms
        pos = self.positions.get(sym)
        self.last.setdefault(sym, bar.open)
        buy = cmd.side == "BUY"
        px = self.costs.buy(bar.open) if buy else self.costs.sell(bar.open)
        if not cmd.reduce_only:                                           # --- open
            if pos is not None:
                self._reject(cmd, t, "position_exists")
                return
            notional = cmd.size * px
            fee = self.costs.fee_rate * notional
            if buy and self.cash < notional + fee:
                self._reject(cmd, t, "insufficient_cash")
                return
            self.cash += (-notional if buy else notional) - fee
            self.positions[sym] = PaperPosition(cmd.position_side, cmd.size, px, cmd.stop_price, t, fee)
            self.stats["opened"] += 1
            self._emit("fill", t, sym, action="open", side=cmd.position_side, qty=cmd.size, price=px, fee=fee,
                       stop=cmd.stop_price, key=cmd.idempotency_key)
            return
        if pos is None or pos.side != cmd.position_side:                  # --- reduce-only
            self._reject(cmd, t, "no_matching_position")
            return
        self._close(sym, pos, min(cmd.size, pos.qty), px, t, "exit", cmd.idempotency_key)

    def _close(self, sym: str, pos: PaperPosition, qty: float, px: float, t: int, reason: str,
               key: Optional[str] = None) -> None:
        notional = qty * px
        fee = self.costs.fee_rate * notional
        share = qty / pos.qty
        entry_fee = pos.entry_fee * share
        gross = (px - pos.entry) * qty if pos.side == "LONG" else (pos.entry - px) * qty
        pnl = gross - fee - entry_fee
        self.cash += (notional if pos.side == "LONG" else -notional) - fee
        pos.qty -= qty
        pos.entry_fee -= entry_fee
        if pos.qty <= 1e-12:
            del self.positions[sym]
        self.trades.append({"symbol": sym, "side": pos.side, "qty": qty, "entry": pos.entry, "exit": px,
                            "pnl": pnl, "fees": fee + entry_fee, "reason": reason, "closed_ms": t})
        self.stats["closed"] += 1
        if reason == "stop":
            self.stats["stopped"] += 1
        self._emit("fill", t, sym, action="close", reason=reason, side=pos.side, qty=qty, price=px,
                   fee=fee, pnl=pnl, key=key)

    def _check_stop(self, bar: Bar) -> None:
        pos = self.positions.get(bar.symbol)
        if pos is None or pos.stop is None:
            return
        if pos.side == "LONG" and bar.low <= pos.stop:
            px = self.costs.sell(min(pos.stop, bar.open))              # a gap through the stop fills at the open
        elif pos.side == "SHORT" and bar.high >= pos.stop:
            px = self.costs.buy(max(pos.stop, bar.open))
        else:
            return
        self._close(bar.symbol, pos, pos.qty, px, bar.end_ms, "stop")

    # ------------------------------------------------------------------ log

    def _emit(self, event: str, ts_ms: int, symbol: str, **fields) -> None:
        if self.sink is None:
            return
        self.sink.write({"mode": "PAPER_V2", "type": "paper_event", "real": False, "label": LABEL,
                         "event": event, "ts_event_ms": ts_ms, "symbol": symbol,
                         "equity": self.equity(), **fields})
