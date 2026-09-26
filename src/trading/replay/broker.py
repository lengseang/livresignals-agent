"""Paper execution and one shared account. Simulated fills only; there is no order path here.

Fills happen at the OPEN of the next 1m bar after a signal, adjusted by half the quoted spread
plus slippage, and pay a fee on each side. Records mirror the live ledger's record types
(`signal` on entry, `close` on exit; see scripts/signal_ledger.py) and carry mode="REPLAY".
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from datetime import datetime, timezone

from trading.replay.bars import Bar
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


class PaperAccount:
    def __init__(self, equity: float, slots: int, costs: CostModel, name: str = ""):
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

    # --- orders ------------------------------------------------------------------
    def submit(self, sig: Signal) -> None:
        if any(p.symbol == sig.symbol and p.action == sig.action for p in self.pending):
            self._skip(sig, "duplicate pending")
            return
        self.pending.append(sig)

    def on_open(self, bar: Bar) -> None:
        """Fill pending orders for bar.symbol at this bar's open (the first price after the signal)."""
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
        self.events.append({"mode": MODE, "type": "skip", "ts": iso(sig.ts_ms), "ts_ms": sig.ts_ms,
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
        price = self.costs.buy(bar.open)
        qty = budget / (price * (1 + self.costs.fee_rate))
        fee = qty * price * self.costs.fee_rate
        self.cash -= budget
        sid = f"{sig.strategy}:{sig.symbol}:{bar.open_ms}"
        self.positions[sig.symbol] = {"id": sid, "qty": qty, "entry": price, "entry_mid": bar.open,
                                      "opened_ms": bar.open_ms, "signal_ms": sig.ts_ms, "spent": budget,
                                      "fee": fee}
        self.last[sig.symbol] = bar.open
        self.fills += 1
        self.events.append({"mode": MODE, "type": "signal", "ts": iso(bar.open_ms), "ts_ms": bar.open_ms,
                            "payload": {"id": sid, "source": f"replay:{sig.strategy}", "symbol": sig.symbol,
                                        "side": sig.side, "entry": price, "opened": iso(bar.open_ms),
                                        "qty": qty, "fee": fee, "signal_ts": iso(sig.ts_ms),
                                        "why": sig.reason, "account": self.name}})

    def _close(self, sig: Signal, bar: Bar) -> None:
        pos = self.positions.pop(sig.symbol, None)
        if pos is None:
            return self._skip(sig, "not held")
        price = self.costs.sell(bar.open)
        proceeds = pos["qty"] * price
        fee = proceeds * self.costs.fee_rate
        self.cash += proceeds - fee
        self.last[sig.symbol] = bar.open
        self.fills += 1
        gross = bar.open / pos["entry_mid"] - 1
        net = (proceeds - fee) / pos["spent"] - 1
        t = {"id": pos["id"], "symbol": sig.symbol, "signal_ms": pos["signal_ms"], "opened_ms": pos["opened_ms"],
             "closed_ms": bar.open_ms, "entry": pos["entry"], "exit": price, "qty": pos["qty"],
             "fee": pos["fee"] + fee, "gross_ret": gross, "net_ret": net, "pnl": proceeds - fee - pos["spent"]}
        self.trades.append(t)
        self.events.append({"mode": MODE, "type": "close", "ts": iso(bar.open_ms), "ts_ms": bar.open_ms,
                            "payload": {"id": pos["id"], "symbol": sig.symbol, "exit": price, "ret": net, "gross_ret": gross,
                                        "net_ret": net, "pnl": t["pnl"], "fee": fee, "fee_total": t["fee"],
                                        "why": sig.reason, "closed_ts": iso(bar.open_ms),
                                        "held_days": (bar.open_ms - pos["opened_ms"]) / 86_400_000,
                                        "account": self.name}})

    # --- marking -----------------------------------------------------------------
    def mark(self, bar: Bar) -> None:
        if bar.symbol in self.positions:
            self.last[bar.symbol] = bar.close

    def equity(self) -> float:
        return self.cash + sum(p["qty"] * self.last[s] for s, p in self.positions.items())

    def liquidation_value(self) -> float:
        """Equity if every position were sold at the last mid, paying spread, slippage and fee."""
        f = self.costs.fee_rate
        return self.cash + sum(p["qty"] * self.costs.sell(self.last[s]) * (1 - f) for s, p in self.positions.items())

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
