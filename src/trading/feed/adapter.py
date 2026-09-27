"""The ONE place where source records become feed v1 payloads (docs/feed/v1.md, docs/feed/schema/v1/).

Every T6.1-vs-source mismatch is settled here and nowhere else:
- a field the source cannot know is `null` AND named in the payload's `unknown` list (its status);
- every source key is either mapped to a v1 field or copied under `extra`: nothing is silently dropped;
- a source record that is not `simulated: true`, or is from the other mode, is refused (returns None).

Functions return `(type, ts_event_ms, data)`; the channel wraps them in envelopes and assigns `seq`.
No number here is invented: derived values (fee_bp, cost_bp, spread_bp) are arithmetic on source fields only.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

SCHEMA_VERSION = 1
MODES = ("LIVE_PAPER", "REPLAY")
GATE_MAP = {"PASS": "PASSED", "FAIL": "FAILED", "INSUFFICIENT_DATA": "INSUFFICIENT"}
_SIDE = {"L": "LONG", "S": "SHORT", "LONG": "LONG", "SHORT": "SHORT"}

# T6.1 §4.3 signal fields the scanner/replay cannot fill yet (null + listed in `unknown` when null)
_SIGNAL_NULLABLE = ("class", "tp_pct", "sl_pct", "expected_move_pct", "cost_rt_pct", "p_star")


def envelope(typ: str, data: dict, *, seq: int, mode: str, run_id: str, ts_event_ms, ts_emit_ms: int) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    return {"v": SCHEMA_VERSION, "type": typ, "seq": seq, "ts_emit_ms": ts_emit_ms, "ts_event_ms": ts_event_ms,
            "mode": mode, "simulated": True, "run_id": run_id, "data": data}


def _unknown(d: dict, names) -> list[str]:
    return [n for n in names if d.get(n) is None]


def iso_ms(s: str | None) -> int | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def params_hash(params) -> str:
    return hashlib.sha256(json.dumps(params or {}, sort_keys=True).encode()).hexdigest()[:12]


def _registry_meta(strategy: str) -> dict:
    """Descriptive metadata from the scanner registry (paper-only module); UNKNOWN if not registered."""
    try:
        from trading.scanner.registry import DEFAULT
        info = DEFAULT.get(strategy)
    except (KeyError, ImportError):
        return {"family": "UNKNOWN", "version": "UNKNOWN", "gate_status": "PENDING", "evidence": "UNKNOWN"}
    return {"family": info.family, "version": info.version, "gate_status": info.gate_status,
            "evidence": info.evidence}


# ----- signal ---------------------------------------------------------------------------------------
_SCANNER_MAPPED = {"signal_id", "strategy_id", "strategy_version", "family", "params", "symbol", "side", "action",
                   "interval", "ref_price", "reason", *_SIGNAL_NULLABLE, "gate_status_at_fire", "evidence",
                   "latency_ms"}


def scanner_signal(rec: dict, expect_mode: str | None = None):
    """A trading.scanner record (already a T6.1-shaped envelope) -> v1 signal data."""
    if rec.get("simulated") is not True or rec.get("type") != "signal" or rec.get("v") != 1:
        return None
    if expect_mode is not None and rec.get("mode") != expect_mode:
        return None
    s = rec.get("data") or {}
    d = {k: s.get(k) for k in _SCANNER_MAPPED}
    d["side"] = _SIDE.get(s.get("side"), s.get("side"))
    d["latency_ms"] = s.get("latency_ms") or {"tick_to_signal": None}
    d["source"] = {"kind": "scanner", "seq": rec.get("seq"), "run_id": rec.get("run_id"),
                   "ts_emit_ms": rec.get("ts_emit_ms")}
    d["extra"] = {k: v for k, v in s.items() if k not in _SCANNER_MAPPED}
    d["unknown"] = _unknown(d, _SIGNAL_NULLABLE + ("strategy_version",))
    return "signal", rec.get("ts_event_ms"), d


def shadow_record(lane: str, strategy: str, rec: dict):
    """P4a: one shadow-chain record -> a v1 envelope tuple, or None. Signals keep the scanner mapping; fills map to
    paper_fill (a rebalance purchase is an OPEN, a sale arrives as a close); gaps/errors become data_quality."""
    chain = {"lane": lane, "chain_strategy": strategy, "chain_seq": rec.get("seq"), "chain_hash": rec.get("hash")}
    t = rec.get("type")
    if t == "signal":
        r = scanner_signal(rec, expect_mode="LIVE_PAPER")
        if r is None:
            return None
        typ, ts, d = r
        d["source"] = {**d["source"], "kind": "shadow"}
        d["extra"] = {**d["extra"], **chain}
        return typ, ts, d
    if t == "shadow_fill":
        p = rec.get("data") or {}
        ft = p.get("fill_type")
        opening = ft in ("signal", "rebalance")
        price = p.get("entry") if ft == "signal" else p.get("price") if ft == "rebalance" else p.get("exit")
        qty, fee = p.get("qty"), p.get("fee")
        notional = None if price is None or qty is None else price * qty
        d = {"fill_id": f"shadow:{strategy}:{rec.get('seq')}", "signal_id": p.get("id"), "strategy_id": strategy,
             "symbol": p.get("symbol"), "side": "BUY" if opening else "SELL", "intent": "OPEN" if opening else "CLOSE",
             "status": "FILLED", "liquidity": None, "ref_price": None, "fill_price": price, "qty": qty,
             "fee_quote": fee, "fee_bp": (fee / notional * 1e4) if (fee is not None and notional) else None,
             "spread_paid_bp": None, "slippage_bp": None, "modelled_latency_ms": None,
             "model_version": "shadow-paper-v1", "account": p.get("account"), "flags": [],
             "extra": {**{k: v for k, v in p.items() if k not in ("symbol", "qty", "fee", "account", "id")}, **chain}}
        d["unknown"] = _unknown(d, _FILL_NULLABLE)
        return "paper_fill", rec.get("ts_event_ms"), d
    if t in ("gap", "error"):
        p = rec.get("data") or {}
        return ("data_quality", rec.get("ts_event_ms"), {
            "kind": "OTHER", "symbol": None, "stream": f"shadow:{lane}:{strategy}",
            "detail": (f"shadow {t}: {p.get('reason') or p.get('error')}"), "gap": None, "extra": {**p, **chain}})
    return None                                   # shadow_mark: daily equity, served by /v1/shadow instead


def replay_strategy_signal(ev: dict, run_id: str):
    p = ev["payload"]
    meta = _registry_meta(p.get("strategy"))
    mapped = {"id", "strategy", "params", "symbol", "side", "action", "reason", "ref_price", "interval", "run_id"}
    d = {"signal_id": p.get("id"), "strategy_id": p.get("strategy"), "strategy_version": meta["version"],
         "family": meta["family"], "params": p.get("params"), "symbol": p.get("symbol"),
         "side": _SIDE.get(p.get("side"), p.get("side")), "action": p.get("action"), "interval": p.get("interval"),
         "ref_price": p.get("ref_price"), "reason": p.get("reason"),
         **{k: None for k in _SIGNAL_NULLABLE},
         # the gate runs after a replay, so at fire time the registry's status is what applied
         "gate_status_at_fire": meta["gate_status"], "evidence": meta["evidence"],
         "latency_ms": {"tick_to_signal": None},
         "source": {"kind": "replay", "seq": None, "run_id": p.get("run_id", run_id), "ts_emit_ms": None},
         "extra": {k: v for k, v in p.items() if k not in mapped}}
    d["unknown"] = _unknown(d, _SIGNAL_NULLABLE + ("strategy_version",))
    return "signal", ev.get("ts_ms"), d


# ----- replay fills and positions --------------------------------------------------------------------
_OPEN_MAPPED = {"id", "symbol", "side", "entry", "qty", "fee", "account", "flags"}
_CLOSE_MAPPED = {"id", "symbol", "exit", "fee", "account", "flags", "gross_ret", "net_ret"}
_FILL_NULLABLE = ("ref_price", "liquidity", "fee_bp", "spread_paid_bp", "slippage_bp", "modelled_latency_ms")


def _model_version(p: dict, broker: str | None) -> str:
    if p.get("paper"):
        return "replay-broker:paper"
    return f"replay-broker:{broker}" if broker else "UNKNOWN"


def _latency_ms(p: dict):
    s = (p.get("fill_model") or {}).get("latency_order_s")
    return None if s is None else s * 1000


class ReplayEventAdapter:
    """Stateful over one run's events.jsonl (sorted by time): closes need their open (side, qty, entry)."""

    def __init__(self, run_id: str, broker: str | None, ref_prices: dict[str, float]):
        self.run_id, self.broker, self.ref = run_id, broker, ref_prices
        self.open: dict[tuple, dict] = {}

    def convert(self, ev: dict) -> list[tuple]:
        t = ev.get("type")
        if t == "strategy_signal":
            return [replay_strategy_signal(ev, self.run_id)]
        if t == "signal":
            return self._open(ev)
        if t == "close":
            return self._close(ev)
        return [("data_quality", ev.get("ts_ms"), {
            "kind": "OTHER", "symbol": None, "stream": "replay_events",
            "detail": f"unmapped replay event type {t!r}; kept under extra", "gap": None, "extra": ev})]

    def _strategy(self, p: dict) -> str:
        src = p.get("source") or ""
        return src.split(":", 1)[1] if src.startswith("replay:") else (p.get("id") or "").split(":", 1)[0]

    def _fill(self, p, *, intent, side, price, qty, ts, strategy_id):
        notional = None if price is None or qty is None else price * qty
        fee = p.get("fee")
        d = {"fill_id": f"{p.get('id')}:{p.get('account')}:{intent}", "signal_id": p.get("id"),
             "strategy_id": strategy_id,
             "symbol": p.get("symbol"), "side": side, "intent": intent, "status": "FILLED", "liquidity": None,
             "ref_price": self.ref.get(p.get("id")) if intent == "OPEN" else None,
             "fill_price": price, "qty": qty, "fee_quote": fee,
             "fee_bp": (fee / notional * 1e4) if (fee is not None and notional) else None,
             "spread_paid_bp": None, "slippage_bp": None, "modelled_latency_ms": _latency_ms(p),
             "model_version": _model_version(p, self.broker), "account": p.get("account"),
             "flags": p.get("flags") or []}
        mapped = _OPEN_MAPPED if intent == "OPEN" else _CLOSE_MAPPED
        d["extra"] = {k: v for k, v in p.items() if k not in mapped}
        d["unknown"] = _unknown(d, _FILL_NULLABLE + ("strategy_id",))
        return "paper_fill", ts, d

    def _position(self, o: dict, *, state, ts, p=None):
        d = {"position_id": f"{o['signal_id']}:{o['account']}", "strategy_id": o["strategy_id"],
             "symbol": o["symbol"], "side": o["side"], "state": state, "qty": o["qty"],
             "entry_price": o["entry"], "mark_price": None, "opened_ms": o["opened_ms"], "closed_ms": None,
             "pnl_gross_pct": None, "pnl_net_pct": None, "cost_bp": None, "cost_share": None,
             "tp_price": None, "sl_price": None, "exit_reason": None, "account": o["account"]}
        if p is not None:
            g, n = p.get("gross_ret"), p.get("net_ret")
            d.update(closed_ms=ts, exit_reason=p.get("why"), mark_price=p.get("exit"),
                     pnl_gross_pct=None if g is None else g * 100, pnl_net_pct=None if n is None else n * 100)
            if g is not None and n is not None:
                d["cost_bp"] = (g - n) * 1e4
                d["cost_share"] = (g - n) / abs(g) if g else None
        d["unknown"] = _unknown(d, ("mark_price", "pnl_gross_pct", "pnl_net_pct", "cost_bp", "cost_share",
                                    "tp_price", "sl_price", "exit_reason", "closed_ms"))
        return "position", ts, d

    def _open(self, ev):
        p, ts = ev["payload"], ev.get("ts_ms")
        side = _SIDE.get(p.get("side"), p.get("side"))
        fill = self._fill(p, intent="OPEN", side="BUY" if side == "LONG" else "SELL",
                          price=p.get("entry"), qty=p.get("qty"), ts=ts, strategy_id=self._strategy(p))
        o = {"signal_id": p.get("id"), "account": p.get("account"), "strategy_id": fill[2]["strategy_id"],
             "symbol": p.get("symbol"), "side": side, "qty": p.get("qty"), "entry": p.get("entry"), "opened_ms": ts}
        self.open[(p.get("account"), p.get("id"))] = o
        return [fill, self._position(o, state="OPEN", ts=ts)]

    def _close(self, ev):
        p, ts = ev["payload"], ev.get("ts_ms")
        o = self.open.pop((p.get("account"), p.get("id")), None)
        side = None if o is None else ("SELL" if o["side"] == "LONG" else "BUY")
        fill = self._fill(p, intent="CLOSE", side=side, price=p.get("exit"), qty=None if o is None else o["qty"],
                          ts=ts, strategy_id=None if o is None else o["strategy_id"])
        if o is None:
            fill[2]["unknown"] = sorted(set(fill[2]["unknown"]) | {"side", "qty"})
            return [fill]
        return [fill, self._position(o, state="CLOSED", ts=ts, p=p)]


# ----- strategy_status --------------------------------------------------------------------------------
_EVIDENCE_NULLABLE = ("is_window", "oos_window", "win_rate_pct", "win_rate_basis", "p_star_pct", "tp_pct",
                      "sl_pct", "cost_rt_pct", "expectancy_net_pct", "expectancy_gross_pct", "profit_factor_net",
                      "sharpe_oos", "sr_per_trade_oos", "dsr", "dsr_trials", "max_dd_pct")


def _empty_evidence(source: str) -> dict:
    ev = {k: None for k in _EVIDENCE_NULLABLE}
    ev.update(source=source, trades={"is": None, "oos": None}, oos_status="NOT_RUN",
              vs_btc={"strategy_ret_pct": None, "btc_ret_pct": None, "beats": None},
              regime={"trend": None, "vol": None, "label": None})
    return ev


def _pct(x):
    return None if x is None else x * 100


def _regime(label: str | None) -> dict:
    if not label:
        return {"trend": None, "vol": None, "label": None}
    trend = "trend" if ("bull" in label or "bear" in label) else "range" if "range" in label else None
    vol = "high" if "high-vol" in label else "low" if "low-vol" in label else None
    return {"trend": trend, "vol": vol, "label": label}


def strategy_row_from_run(run_id: str, summary: dict, gate: dict | None) -> dict:
    """REPLAY row: evidence comes only from gate.json (+ window/max DD from summary.json). No gate => PENDING."""
    strat = summary.get("strategy") or (gate or {}).get("strategy") or "UNKNOWN"
    meta = _registry_meta(strat)
    params = summary.get("params") or (gate or {}).get("params") or {}
    ev = _empty_evidence("REPLAY")
    win = summary.get("window") or {}
    # older summaries lack is_oos_split; the gate records the split it used in oos_window.from
    split_ms = iso_ms(win.get("is_oos_split") or ((gate or {}).get("oos_window") or {}).get("from"))
    t0, t1 = iso_ms(win.get("trade_from")), iso_ms(win.get("to"))
    if t0 is not None and split_ms is not None:
        ev["is_window"] = {"from_ms": t0, "to_ms": split_ms}
    if split_ms is not None and t1 is not None:
        ev["oos_window"] = {"from_ms": split_ms, "to_ms": t1}
    oos_period = (((summary.get("accounts") or {}).get("net") or {}).get("periods") or {}).get("OOS") or {}
    if gate is None:
        status, reasons, evaluated = "PENDING", ["no gate.json for this run: the T3.3 gate has not evaluated it"], None
    else:
        status = GATE_MAP.get(gate.get("verdict"), "PENDING")
        reasons = list(gate.get("reasons") or [])
        evaluated = iso_ms(gate.get("gated_at"))
        c = gate.get("checks") or {}
        te, dsr = c.get("trade_economics") or {}, c.get("dsr") or {}
        ev["trades"]["oos"] = (c.get("oos_trades") or {}).get("value")
        ev["cost_rt_pct"] = _pct(te.get("round_trip_cost"))
        ev["expectancy_net_pct"] = _pct(te.get("mean_net_return_per_trade"))
        ev["expectancy_gross_pct"] = _pct(te.get("mean_gross_return_per_trade"))
        wr = _pct(te.get("win_rate_net"))
        # never a bare win rate: it travels with p* (fixed exits) or with mean-net-vs-cost (the gate's other mode)
        if te.get("mode") == "fixed_exits" and te.get("breakeven_win_rate") is not None:
            ev.update(win_rate_pct=wr, win_rate_basis="p_star", p_star_pct=_pct(te["breakeven_win_rate"]),
                      tp_pct=_pct(te.get("tp")), sl_pct=_pct(te.get("sl")))
        elif te.get("mode") == "mean_net_vs_cost" and ev["expectancy_net_pct"] is not None \
                and ev["cost_rt_pct"] is not None:
            ev.update(win_rate_pct=wr, win_rate_basis="mean_net_vs_cost")
        ev["dsr"], ev["dsr_trials"] = dsr.get("value"), dsr.get("n_trials")
        ev["sr_per_trade_oos"] = dsr.get("sr_per_trade")
        oos_net = (c.get("oos_net") or {}).get("value")
        if ev["trades"]["oos"] is not None and ev["trades"]["oos"] < (c.get("oos_trades") or {}).get("min", 0):
            ev["oos_status"] = "INSUFFICIENT"
        elif oos_net is not None:
            ev["oos_status"] = "POSITIVE" if oos_net > 0 else "NEGATIVE"
        b = c.get("btc_benchmark") or {}
        ev["vs_btc"] = {"strategy_ret_pct": _pct(b.get("strategy_oos_net")),
                        "btc_ret_pct": _pct(b.get("btc_buy_hold_oos_net")), "beats": b.get("beats_btc")}
        ev["regime"] = _regime((c.get("regime") or {}).get("label"))
        ev["max_dd_pct"] = _pct(oos_period.get("max_dd"))
    ev["unknown"] = _unknown(ev, _EVIDENCE_NULLABLE) + (["trades.is"] if ev["trades"]["is"] is None else [])
    return {"strategy_id": strat, "run_id": run_id, "family": meta["family"], "version": meta["version"],
            "params": params, "params_hash": params_hash(params), "symbols": summary.get("symbols") or [],
            "timeframe": params.get("interval"), "state": None,
            "gate": {"status": status, "reasons": reasons, "evaluated_ms": evaluated},
            "evidence": ev, "live": None,
            "caveats": list((gate or summary).get("caveats") or []),
            "unknown": ["state"]}


def strategy_row_from_registry(info, signals_24h: int | None, last_signal_ms: int | None) -> dict:
    """LIVE_PAPER row from the scanner registry. There is no live evidence yet: every evidence field is null."""
    ev = _empty_evidence("LIVE_PAPER")
    ev["unknown"] = list(_EVIDENCE_NULLABLE) + ["trades.is", "trades.oos"]
    return {"strategy_id": info.name, "run_id": None, "family": info.family, "version": info.version,
            "params": dict(info.params), "params_hash": params_hash(info.params), "symbols": [],
            "timeframe": None, "state": None,
            "gate": {"status": info.gate_status,
                     "reasons": [f"registry gate_status {info.gate_status}; evidence pointer: {info.evidence}"],
                     "evaluated_ms": None},
            "evidence": ev,
            "live": {"signals_24h": signals_24h, "last_signal_ms": last_signal_ms, "open_positions": None,
                     "pnl_net_pct_24h": None},
            "caveats": [info.description] if info.description else [],
            "unknown": ["state", "symbols", "timeframe", "live.open_positions", "live.pnl_net_pct_24h"]}


# ----- recorder: quotes, data quality -------------------------------------------------------------------
def quote_item(symbol: str, book: dict | None, trade: dict | None, gap_open: bool) -> dict:
    bid = ask = recv = None
    if book:
        bid, ask, recv = book["data"].get("bid"), book["data"].get("ask"), book.get("ts_recv_ns")
    spread = None
    if bid and ask:
        spread = (ask - bid) / ((ask + bid) / 2) * 1e4
    d = {"symbol": symbol, "bid": bid, "ask": ask,
         "last": trade["data"].get("price") if trade else None, "spread_bp": spread,
         "chg_1m_pct": None, "chg_1h_pct": None, "msgs_per_s": None,
         "quote_recv_ms": None if recv is None else recv // 1_000_000,
         "trade_ms": trade["data"].get("trade_ms") if trade else None, "gap_open": gap_open}
    d["unknown"] = _unknown(d, ("bid", "ask", "last", "spread_bp", "chg_1m_pct", "chg_1h_pct", "msgs_per_s",
                                "quote_recv_ms", "trade_ms"))
    return d


def data_quality_from_gap(rec: dict):
    kind = rec.get("kind")
    recv = rec.get("ts_recv_ns")
    base = {"recv_ms": None if recv is None else recv // 1_000_000}
    if kind == "gap":
        d = {"kind": "GAP", "symbol": rec.get("symbol"), "stream": "aggTrade",
             "detail": f"{rec.get('missing')} aggTrade ids never received (recorded, not interpolated)",
             "gap": {"from_id": rec.get("from_id"), "to_id": rec.get("to_id"), "missing": rec.get("missing")}}
    elif kind == "reconnect":
        d = {"kind": "RECONNECT", "symbol": None, "stream": None,
             "detail": "websocket reconnect: trades during the outage are unknown", "gap": None}
    else:
        d = {"kind": "OTHER", "symbol": rec.get("symbol"), "stream": None,
             "detail": f"unmapped recorder gap record kind {kind!r}", "gap": None}
    mapped = {"kind", "symbol", "from_id", "to_id", "missing", "ts_recv_ns"}
    d.update(base, extra={k: v for k, v in rec.items() if k not in mapped})
    return "data_quality", None, d        # recorder gaps have receive time only, no market time
