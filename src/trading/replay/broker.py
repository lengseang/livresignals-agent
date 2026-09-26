"""Paper execution and one shared account. Simulated fills only; there is no order path here.

Fills happen at the OPEN of the next 1m bar after a signal, adjusted by half the quoted spread
plus slippage, and pay a fee on each side. Records mirror the live ledger's record types
(`signal` on entry, `close` on exit; see scripts/signal_ledger.py) and carry mode="REPLAY".

Optional exits (docs/loop/designs/T3.4.md): a signal's sl/tp become stop and take-profit prices around the
entry bar's open, checked at every 1m close with the T4.1 kline rules; max_hold_ms is a time stop filled at
the next 1m open. side="S" opens a SHORT, a PAPER-ONLY simulation (spot cannot short; borrow/funding cost
and liquidation are not modelled).
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from trading.replay.bars import Bar, BarOpen
from trading.replay.context import LookAheadError
from trading.replay.strategy import Signal

MODE = "REPLAY"


def iso(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class CostModel:
    fee_rate: float = 0.001        # VIP 0 spot, 0.10% per side (the account's real rate is UNKNOWN)
    spread_bps: float = 0.0        # full quoted spread; each side pays half (UNKNOWN until T2.3 measures it)
    slippage_bps: float = 0.0      # per side, on top of the half spread

    @property
    def side_frac(self) -> float:
        return (self.spread_bps / 2 + self.slippage_bps) / 1e4

    def buy(self, mid: float) -> float:
        return mid * (1 + self.side_frac)

    def sell(self, mid: float) -> float:
        return mid * (1 - self.side_frac)


class Broker(Protocol):
    """What the replay engine needs from an account. PaperAccount (legacy flat cost model) and
    trading.paper.broker.KlineBroker (measured fill model, T4.2) both implement it; strategies never
    see it except through ctx.position()."""
    positions: dict
    fills: int
    start_equity: float
    curve: array

    def submit(self, sig: Signal) -> None: ...
    def on_open(self, bar: Bar) -> None: ...
    def mark(self, bar: Bar) -> None: ...
    def record(self) -> None: ...
    def equity(self) -> float: ...


class PaperAccount:
    def __init__(self, equity: float, slots: int, costs: CostModel, name: str = "", mode: str = MODE):
        self.mode = mode               # "REPLAY", or "LIVE_PAPER" when the live scanner owns the account
        self.start_equity = self.cash = float(equity)
        self.slots, self.costs, self.name = slots, costs, name
        self.positions: dict[str, dict] = {}
        self.last: dict[str, float] = {}
        self.pending: list[Signal] = []
        self.trades: list[dict] = []
        self.events: list[dict] = []
        self.fills = 0
        self.curve = array("d")
        self._peak = self._mdd = 0.0

    # --- pricing hooks: a fill model overrides these (trading.paper.broker.KlineBroker) -----
    def buy_price(self, symbol: str, mid: float) -> float:
        return self.costs.buy(mid)

    def sell_price(self, symbol: str, mid: float) -> float:
        return self.costs.sell(mid)

    def size_budget(self, symbol: str, budget: float) -> tuple[float, list]:
        """Entry budget after any per-fill cap, plus flags for the fill record."""
        return budget, []

    def exit_flags(self, symbol: str, notional: float) -> list:
        return []

    def fill_meta(self, flags: list) -> dict:
        """Extra fields on every fill record (the legacy model adds none)."""
        return {}

    # --- orders ------------------------------------------------------------------
    def submit(self, sig: Signal) -> None:
        if any(p.symbol == sig.symbol and p.action == sig.action for p in self.pending):
            self._skip(sig, "duplicate pending")
            return
        self.pending.append(sig)

    def on_open(self, bar: Bar | BarOpen) -> None:
        """Fill pending orders for bar.symbol at this bar's open (the first price after the signal).

        Only symbol, open_ms and open are read, so a live BarOpen (all that is known at the open) works.
        """
        if not self.pending:
            return
        keep = []
        for sig in self.pending:
            if sig.symbol != bar.symbol:
                keep.append(sig)
                continue
            if bar.open_ms < sig.ts_ms:
                raise LookAheadError(f"fill at {bar.open_ms} precedes its signal at {sig.ts_ms}")
            (self._open if sig.action == "open" else self._close)(sig, bar)
        self.pending = keep

    def _skip(self, sig: Signal, why: str) -> None:
        self.events.append({"mode": self.mode, "type": "skip", "ts": iso(sig.ts_ms), "ts_ms": sig.ts_ms,
                            "payload": {"symbol": sig.symbol, "action": sig.action, "why": why,
                                        "strategy": sig.strategy, "account": self.name}})

    def _open(self, sig: Signal, bar: Bar) -> None:
        if sig.symbol in self.positions:
            return self._skip(sig, "already held")
        if len(self.positions) >= self.slots:
            return self._skip(sig, "no free slot")
        self.last.setdefault(sig.symbol, bar.open)
        budget = min(self.cash, self.equity() / self.slots)
        if budget <= 0:
            return self._skip(sig, "no cash")
        short = sig.side == "S"
        # a short (PAPER-ONLY) sells at the bid side: same fill model, opposite direction
        price = self.sell_price(sig.symbol, bar.open) if short else self.buy_price(sig.symbol, bar.open)
        budget, flags = self.size_budget(sig.symbol, budget)
        qty = budget / (price * (1 + self.costs.fee_rate))
        fee = qty * price * self.costs.fee_rate
        self.cash -= budget
        sid = f"{sig.strategy}:{sig.symbol}:{bar.open_ms}"
        ref, sgn = bar.open, (-1 if short else 1)
        stop = None if sig.sl is None else ref * (1 - sgn * sig.sl)
        take = None if sig.tp is None else ref * (1 + sgn * sig.tp)
        deadline = None if sig.max_hold_ms is None else bar.open_ms + sig.max_hold_ms
        self.positions[sig.symbol] = {"id": sid, "qty": qty, "entry": price, "entry_mid": bar.open,
                                      "opened_ms": bar.open_ms, "signal_ms": sig.ts_ms, "spent": budget,
                                      "fee": fee, "side": sig.side, "strategy": sig.strategy,
                                      "stop": stop, "take_profit": take, "deadline_ms": deadline}
        self.last[sig.symbol] = bar.open
        self.fills += 1
        self.events.append({"mode": self.mode, "type": "signal", "ts": iso(bar.open_ms), "ts_ms": bar.open_ms,
                            "payload": {"id": sid, "source": f"replay:{sig.strategy}", "symbol": sig.symbol,
                                        "side": sig.side, "entry": price, "opened": iso(bar.open_ms),
                                        "qty": qty, "fee": fee, "signal_ts": iso(sig.ts_ms),
                                        "why": sig.reason, "account": self.name,
                                        **({"stop": stop, "take_profit": take, "deadline_ms": deadline}
                                           if (stop or take or deadline) else {}),
                                        **({"paper_only_short": True} if short else {}),
                                        **self.fill_meta(flags)}})

    def _close(self, sig: Signal, bar: Bar) -> None:
        pos = self.positions.pop(sig.symbol, None)
        if pos is None:
            return self._skip(sig, "not held")
        short = pos.get("side") == "S"
        price = self.buy_price(sig.symbol, bar.open) if short else self.sell_price(sig.symbol, bar.open)
        self._settle(sig.symbol, pos, price, bar.open, bar.open_ms, sig.reason, self.costs.fee_rate)

    def _settle(self, symbol: str, pos: dict, price: float, mid: float, ts_ms: int, why: str,
                fee_rate: float) -> None:
        """Book an exit at `price` (fill) whose reference mid is `mid`, known at ts_ms."""
        short = pos.get("side") == "S"
        qty = pos["qty"]
        if short:
            proceeds = qty * (2 * pos["entry"] - price)     # collateral back plus (entry - exit) per unit
            gross = 1 - mid / pos["entry_mid"]
        else:
            proceeds = qty * price
            gross = mid / pos["entry_mid"] - 1
        fee = qty * price * fee_rate
        self.cash += proceeds - fee
        self.last[symbol] = mid
        self.fills += 1
        net = (proceeds - fee) / pos["spent"] - 1
        t = {"id": pos["id"], "symbol": symbol, "signal_ms": pos["signal_ms"], "opened_ms": pos["opened_ms"],
             "closed_ms": ts_ms, "entry": pos["entry"], "exit": price, "qty": qty,
             "fee": pos["fee"] + fee, "gross_ret": gross, "net_ret": net, "pnl": proceeds - fee - pos["spent"],
             "why": why, "side": pos.get("side", "L")}
        self.trades.append(t)
        self.events.append({"mode": self.mode, "type": "close", "ts": iso(ts_ms), "ts_ms": ts_ms,
                            "payload": {"id": pos["id"], "symbol": symbol, "exit": price, "ret": net, "gross_ret": gross,
                                        "net_ret": net, "pnl": t["pnl"], "fee": fee, "fee_total": t["fee"],
                                        "why": why, "closed_ts": iso(ts_ms), "side": t["side"],
                                        "held_days": (ts_ms - pos["opened_ms"]) / 86_400_000,
                                        "account": self.name,
                                        **self.fill_meta(self.exit_flags(symbol, qty * price))}})

    # --- stop-loss / take-profit / time stop (docs/loop/designs/T3.4.md, T4.1 kline rules) --------
    def bar_exit(self, bar: Bar, pos: dict) -> tuple[str, float] | None:
        """("stop" | "take_profit", fill price) if this 1m bar exits the position. Stops are taker orders
        (gap-through fills at the open); take-profits are limits needing a strict trade-through, filled at
        the limit; a bar touching both books the stop. KlineBroker overrides this with fills.bar_exit."""
        stop, take = pos["stop"], pos["take_profit"]
        if pos.get("side") == "S":
            if stop is not None and bar.high >= stop:
                return "stop", self.buy_price(bar.symbol, max(stop, bar.open))
            if take is not None and bar.low < take:
                return "take_profit", take
            return None
        if stop is not None and bar.low <= stop:
            return "stop", self.sell_price(bar.symbol, min(stop, bar.open))
        if take is not None and bar.high > take:
            return "take_profit", take
        return None

    def exit_fee_rate(self, reason: str) -> float:
        return self.costs.fee_rate

    def _check_exits(self, bar: Bar, pos: dict) -> None:
        if bar.open_ms < pos["opened_ms"]:
            return                                          # never use a bar that opened before the fill
        if pos["stop"] is not None or pos["take_profit"] is not None:
            hit = self.bar_exit(bar, pos)
            if hit is not None:
                reason, price = hit
                short = pos.get("side") == "S"
                if reason == "stop":
                    mid = max(pos["stop"], bar.open) if short else min(pos["stop"], bar.open)
                else:
                    mid = pos["take_profit"]
                self.positions.pop(bar.symbol)
                self._settle(bar.symbol, pos, price, mid, bar.end_ms,
                             "stop_loss" if reason == "stop" else "take_profit", self.exit_fee_rate(reason))
                return
        dl = pos["deadline_ms"]
        if dl is not None and bar.end_ms >= dl and not any(
                p.symbol == bar.symbol and p.action == "close" for p in self.pending):
            # a market close at the next 1m open, like any other close
            self.pending.append(Signal(pos.get("strategy", "broker"), bar.symbol, "close", bar.end_ms,
                                       "time_stop", bar.close, "1m", side=pos.get("side", "L")))

    # --- marking -----------------------------------------------------------------
    def mark(self, bar: Bar) -> None:
        pos = self.positions.get(bar.symbol)
        if pos is None:
            return
        self.last[bar.symbol] = bar.close
        if pos.get("stop") is not None or pos.get("take_profit") is not None or pos.get("deadline_ms") is not None:
            self._check_exits(bar, pos)

    @staticmethod
    def _value(p: dict, last: float) -> float:
        return p["qty"] * (2 * p["entry"] - last) if p.get("side") == "S" else p["qty"] * last

    def equity(self) -> float:
        return self.cash + sum(self._value(p, self.last[s]) for s, p in self.positions.items())

    def liquidation_value(self) -> float:
        """Equity if every position were closed at the last mid, paying spread, slippage and fee."""
        f = self.costs.fee_rate
        tot = self.cash
        for s, p in self.positions.items():
            if p.get("side") == "S":
                px = self.buy_price(s, self.last[s])
                tot += p["qty"] * (2 * p["entry"] - px) - p["qty"] * px * f
            else:
                tot += p["qty"] * self.sell_price(s, self.last[s]) * (1 - f)
        return tot

    def record(self) -> None:
        e = self.equity() if self.positions else self.cash
        self.curve.append(e)
        if e > self._peak:
            self._peak = e
        elif self._peak > 0:
            dd = 1 - e / self._peak
            if dd > self._mdd:
                self._mdd = dd

    def max_drawdown(self) -> float:
        return self._mdd
