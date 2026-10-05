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
  * An opening order carries `worst_price`; a fill beyond it is refused (gap protection), never accepted.
  * V2-15: an opening order's `target_price` is a resting take-profit. Stop and target touched in the same bar ->
    the STOP (the path inside a bar is unknown; conservative). A gap through the target fills at the open (better).
  * V2-15: with a `ratchet` rule, at every bar close the book tracks the best excursion (MFE) and the worst (MAE)
    and tightens the stop per `trading.v2.position.ratchet`; the new stop applies from the NEXT bar. A locked level
    already through the last price becomes an exit at the next open. Stops only ever tighten (`amend_stop` refuses
    anything else), and every amendment is a Contract K record.
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
from pydantic import ValidationError

from trading.v2.contracts import ExecutionCommand, PositionAdjustment
from trading.v2.ids import derive_id
from trading.v2.position import RatchetRule, ratchet

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
    target: Optional[float] = None
    best: Optional[float] = None    # best price since entry (long: highest high; short: lowest low)
    worst: Optional[float] = None   # worst price since entry

    def mfe(self) -> float:
        """Best excursion as a fraction of entry (>= 0 once a bar has been seen)."""
        if self.best is None:
            return 0.0
        return (self.best / self.entry - 1.0) if self.side == "LONG" else (1.0 - self.best / self.entry)

    def mae(self) -> float:
        """Worst excursion as a fraction of entry (<= 0)."""
        if self.worst is None:
            return 0.0
        return (self.worst / self.entry - 1.0) if self.side == "LONG" else (1.0 - self.worst / self.entry)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _week(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
    return (d - timedelta(days=d.weekday())).isoformat()      # Monday of the UTC week


def read_chain(chain_dir) -> list[dict]:
    """Every record of a VERIFIED chain lane, in order. A broken chain raises: it is never restored from."""
    import json
    from pathlib import Path
    from trading.shadow.chain import verify
    v = verify(chain_dir)
    if not v["ok"]:
        raise ValueError(f"paper chain does not verify: {v['error']}")
    out = []
    for day in sorted(Path(chain_dir).glob("*.jsonl")):
        with open(day, encoding="utf-8") as f:
            out += [json.loads(line) for line in f if line.strip()]
    return out


class PaperBook:
    def __init__(self, start_equity: float, costs: CostModel = CostModel(), sink: Optional[Sink] = None,
                 ratchet: Optional[RatchetRule] = None):
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
        self.ratchet = ratchet
        self.adjustments: list[PositionAdjustment] = []
        self._exit_next: dict[str, str] = {}      # symbol -> reason: close at the next bar's open
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
        """Cancel every queued, unfilled OPENING command (Gate 6: kill switch cancels orders). Reduce-only commands
        stay: they only take risk off (owner decision E14). Returns how many were cancelled."""
        keep, n = [], 0
        for cmd, ts in self.pending:
            if cmd.reduce_only:
                keep.append((cmd, ts))
                continue
            n += 1
            self.stats[f"cancelled:{why}"] += 1
            self._emit("cancelled", ts_ms, cmd.symbol, reason=why, key=cmd.idempotency_key)
        self.pending = keep
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
        why = self._exit_next.pop(bar.symbol, None)
        pos = self.positions.get(bar.symbol)
        if why is not None and pos is not None:            # a queued risk-reducing exit fills at this open
            px = self.costs.sell(bar.open) if pos.side == "LONG" else self.costs.buy(bar.open)
            self._close(bar.symbol, pos, pos.qty, px, bar.open_ms, why)
        keep = []
        for cmd, ts in self.pending:
            if cmd.symbol != bar.symbol or bar.open_ms < ts:
                keep.append((cmd, ts))
                continue
            self._fill(cmd, bar)
        self.pending = keep
        self._check_exits(bar)
        self.last[bar.symbol] = bar.close
        self._manage(bar)
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
            if cmd.worst_price is not None and ((buy and px > cmd.worst_price) or (not buy and px < cmd.worst_price)):
                self._reject(cmd, t, "slippage_exceeded")      # the market moved past the order's protection
                return
            notional = cmd.size * px
            fee = self.costs.fee_rate * notional
            if buy and self.cash < notional + fee:
                self._reject(cmd, t, "insufficient_cash")
                return
            self.cash += (-notional if buy else notional) - fee
            self.positions[sym] = PaperPosition(cmd.position_side, cmd.size, px, cmd.stop_price, t, fee,
                                                target=cmd.target_price)
            self.stats["opened"] += 1
            self._emit("fill", t, sym, action="open", side=cmd.position_side, qty=cmd.size, price=px, fee=fee,
                       stop=cmd.stop_price, target=cmd.target_price, key=cmd.idempotency_key)
            return
        if pos is None or pos.side != cmd.position_side:                  # --- reduce-only
            self._reject(cmd, t, "no_matching_position")
            return
        qty = min(cmd.size, pos.qty)
        self._close(sym, pos, qty, px, t, "exit" if qty >= pos.qty else "reduce", cmd.idempotency_key)

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
                            "pnl": pnl, "fees": fee + entry_fee, "reason": reason, "closed_ms": t,
                            "mfe": pos.mfe(), "mae": pos.mae()})
        self.stats["closed"] += 1
        if reason in ("stop", "target", "ratchet_exit"):
            self.stats[{"stop": "stopped", "target": "target_hit", "ratchet_exit": "ratchet_exit"}[reason]] += 1
        self._emit("fill", t, sym, action="close", reason=reason, side=pos.side, qty=qty, price=px,
                   fee=fee, pnl=pnl, key=key)

    def _check_exits(self, bar: Bar) -> None:
        pos = self.positions.get(bar.symbol)
        if pos is None:
            return
        long = pos.side == "LONG"
        if pos.stop is not None and ((long and bar.low <= pos.stop) or (not long and bar.high >= pos.stop)):
            # checked FIRST: if the target was also touched, which came first is unknown, so assume the worse
            px = self.costs.sell(min(pos.stop, bar.open)) if long else self.costs.buy(max(pos.stop, bar.open))
            self._track(pos, bar)
            self._close(bar.symbol, pos, pos.qty, px, bar.end_ms, "stop")
            return
        if pos.target is not None and ((long and bar.high >= pos.target) or (not long and bar.low <= pos.target)):
            # a gap through the target fills at the open, the better price
            px = self.costs.sell(max(pos.target, bar.open)) if long else self.costs.buy(min(pos.target, bar.open))
            self._track(pos, bar)
            self._close(bar.symbol, pos, pos.qty, px, bar.end_ms, "target")

    @staticmethod
    def _track(pos: PaperPosition, bar: Bar) -> None:
        hi, lo = (bar.high, bar.low) if pos.side == "LONG" else (bar.low, bar.high)
        better = (lambda a, b: a > b) if pos.side == "LONG" else (lambda a, b: a < b)
        if pos.best is None or better(hi, pos.best):
            pos.best = hi
        if pos.worst is None or better(pos.worst, lo):
            pos.worst = lo

    def _manage(self, bar: Bar) -> None:
        """At this bar's close: update MFE/MAE, then let the ratchet tighten the stop for the NEXT bar."""
        pos = self.positions.get(bar.symbol)
        if pos is None:
            return
        self._track(pos, bar)
        if self.ratchet is None or pos.stop is None or bar.symbol in self._exit_next:
            return
        act = ratchet(self.ratchet, side=pos.side, entry=pos.entry, target=pos.target, best=pos.best,
                      stop=pos.stop, last=bar.close)
        if act is None:
            return
        if act.kind == "exit":
            self._exit_next[bar.symbol] = "ratchet_exit"
            self.stats["ratchet_exit_queued"] += 1
            self._emit("ratchet_exit_queued", bar.end_ms, bar.symbol, level=act.level, last=bar.close,
                       rule=self.ratchet.version)
            return
        self.amend_stop(bar.symbol, act.level, bar.end_ms, "ratchet",
                        f"{self.ratchet.version}: MFE {pos.mfe():.2%}, lock {self.ratchet.lock_frac:.0%} of it")

    def amend_stop(self, symbol: str, new_stop: float, ts_ms: int, source: str, reason: str) -> Optional[PositionAdjustment]:
        """Tighten the stop of an open position. Returns the Contract K record, or None if refused (no position,
        no stop, or the change would loosen / cross the last price: the contract refuses those)."""
        pos = self.positions.get(symbol)
        if pos is None or pos.stop is None:
            self.stats["amend_refused:no_position"] += 1
            return None
        try:
            adj = PositionAdjustment(
                contract="position_adjustment", version="1.0",
                adjustment_id=derive_id("padj", symbol, str(ts_ms), source, repr(new_stop)),
                symbol=symbol, position_side=pos.side, kind="TIGHTEN_STOP", old_stop=pos.stop, new_stop=new_stop,
                last_price=self.last.get(symbol, pos.entry), source=source, reason=reason[:300], ts_ms=ts_ms)
        except ValidationError:
            self.stats["amend_refused:not_tighter"] += 1
            return None
        pos.stop = new_stop
        self.adjustments.append(adj)
        self.stats[f"stop_tightened:{source}"] += 1
        self._emit("stop_tightened", ts_ms, symbol, adjustment=adj.to_dict(), best=pos.best, worst=pos.worst)
        return adj

    def queue_exit(self, symbol: str, reason: str) -> bool:
        """Close the whole position at the next bar's open (risk-reducing; used by position advice)."""
        if symbol not in self.positions:
            return False
        self._exit_next[symbol] = reason
        return True

    # ------------------------------------------------------------------ restart

    def restore(self, records: list[dict]) -> dict:
        """Rebuild the book from its own chain records after a restart (Audit V2-15 #1: a restarted runner used to
        start a FRESH book while its chain continued, so open positions, cash and stops silently vanished).

        Replays fills (open/close), stop tightenings and queued ratchet exits through the SAME accounting code, with
        the sink detached so nothing is re-logged. Orders that were queued but never filled are NOT resurrected: they
        are reported so the caller can log them cancelled. Fails closed: whenever the book is flat, the replayed cash
        must equal the equity the chain recorded, else ValueError (a book that cannot be reconciled must not trade).
        """
        if self.trades or self.positions or self.pending or self._keys:
            raise ValueError("restore only into a fresh book")
        sink, self.sink = self.sink, None
        open_keys: dict[str, str] = {}
        n = 0
        try:
            for r in records:
                if r.get("type") != "paper_event" or r.get("real") is not False:
                    raise ValueError("not a paper-book record")
                ev, sym, t = r["event"], r["symbol"], r["ts_event_ms"]
                if t:
                    self._roll(t)
                n += 1
                if ev == "queued":
                    key = r["command"]["idempotency_key"]
                    self._keys.add(key)
                    open_keys[key] = sym
                elif ev in ("rejected", "cancelled", "duplicate_ignored"):
                    open_keys.pop(r.get("key"), None)
                    if r.get("key"):
                        self._keys.add(r["key"])
                elif ev == "fill" and r["action"] == "open":
                    open_keys.pop(r.get("key"), None)
                    qty, px, fee = r["qty"], r["price"], r["fee"]
                    long = r["side"] == "LONG"
                    self.cash += (-qty * px if long else qty * px) - fee
                    self.positions[sym] = PaperPosition(r["side"], qty, px, r.get("stop"), t, fee, target=r.get("target"))
                    self.last[sym] = px
                    self.stats["opened"] += 1
                elif ev == "fill" and r["action"] == "close":
                    open_keys.pop(r.get("key"), None)
                    pos = self.positions.get(sym)
                    if pos is None:
                        raise ValueError(f"close without an open position at seq {r.get('seq')}")
                    self._close(sym, pos, r["qty"], r["price"], t, r["reason"], r.get("key"))
                    self.last[sym] = r["price"]
                elif ev == "stop_tightened":
                    adj = PositionAdjustment.from_dict(r["adjustment"])
                    pos = self.positions.get(sym)
                    if pos is None:
                        raise ValueError(f"stop amendment without a position at seq {r.get('seq')}")
                    pos.stop = adj.new_stop
                    pos.best, pos.worst = r.get("best", pos.best), r.get("worst", pos.worst)
                    self.adjustments.append(adj)
                    self.last[sym] = adj.last_price
                elif ev == "ratchet_exit_queued":
                    self._exit_next[sym] = "ratchet_exit"
                if not self.positions and abs(self.cash - r["equity"]) > 1e-6 * max(1.0, abs(self.cash)):
                    raise ValueError(f"replayed cash {self.cash:.6f} != recorded equity {r['equity']:.6f} at seq {r.get('seq')}")
                self.high_water_mark = max(self.high_water_mark, r["equity"])
            self._exit_next = {k: v for k, v in self._exit_next.items() if k in self.positions}
        finally:
            self.sink = sink
        self.stats["restored_events"] = n
        return {"events": n, "positions": sorted(self.positions), "unfilled_orders_dropped": sorted([k, v] for k, v in open_keys.items())}

    # ------------------------------------------------------------------ log

    def _emit(self, event: str, ts_ms: int, symbol: str, **fields) -> None:
        if self.sink is None:
            return
        self.sink.write({"mode": "PAPER_V2", "type": "paper_event", "real": False, "label": LABEL,
                         "event": event, "ts_event_ms": ts_ms, "symbol": symbol,
                         "equity": self.equity(), **fields})
