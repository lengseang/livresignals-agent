"""One channel per (mode, run_id): a seq counter, a bounded ring buffer of replayable envelopes, and the sources.

seq rules (docs/feed/v1.md §2):
- replayable events (signal, paper_fill, position, strategy_status, data_quality) get a new seq, strictly
  increasing per channel, go into the ring buffer and are sent with SSE `id: <seq>`;
- control events (hello, heartbeat, quotes, resync) are per connection, NOT buffered, sent WITHOUT an SSE id and
  carry the seq of the last replayable event (so Last-Event-ID always names a replayable event and resume is exact).

A channel reads only its own mode's files: a LIVE_PAPER channel never opens replay/ or once-replay/, and a REPLAY
channel never opens signals/<date>.jsonl or ticks/. Records of the other mode are refused by the adapter as well.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from trading.feed import adapter
from trading.feed.sources import DataRoot, GapTail, LineTail, ShadowTail, read_json


class Channel:
    def __init__(self, mode: str, run_id: str, buffer_size: int, clock=time.time):
        self.mode, self.run_id, self.clock = mode, run_id, clock
        self.buf: deque = deque(maxlen=buffer_size)
        self.seq = 0
        self.lock = threading.Lock()
        self.dropped_total = 0          # replayable events a client missed because they left the buffer
        self.refused = 0                # source records refused (wrong mode / not simulated / wrong type)
        self.universe: list[str] = []
        self.fee_model: dict = {}
        self.replay_window: dict | None = None

    # subclasses: _pull() -> list[(type, ts_event_ms, data)]
    def _pull(self) -> list[tuple]:
        return []

    def poll(self) -> None:
        with self.lock:
            for typ, ts, data in self._pull():
                self.seq += 1
                self.buf.append(adapter.envelope(typ, data, seq=self.seq, mode=self.mode, run_id=self.run_id,
                                                 ts_event_ms=ts, ts_emit_ms=int(self.clock() * 1000)))

    @property
    def oldest(self) -> int:
        return self.buf[0]["seq"] if self.buf else self.seq + 1

    def after(self, cursor: int) -> tuple[list[dict], bool]:
        """Buffered envelopes with seq > cursor, and whether some were already evicted (resync needed)."""
        with self.lock:
            if cursor + 1 < self.oldest and cursor < self.seq:
                self.dropped_total += self.oldest - cursor - 1
                return [], True
            return [e for e in self.buf if e["seq"] > cursor], False

    def control(self, typ: str, data: dict, cursor: int, ts_event_ms=None) -> dict:
        return adapter.envelope(typ, data, seq=cursor, mode=self.mode, run_id=self.run_id,
                                ts_event_ms=ts_event_ms, ts_emit_ms=int(self.clock() * 1000))

    def quotes(self) -> dict | None:
        return None


class ReplayChannel(Channel):
    def __init__(self, data: DataRoot, run_id: str, run: dict, buffer_size: int, clock=time.time):
        super().__init__("REPLAY", run_id, buffer_size, clock)
        self.data, self.run, self.loaded = data, run, False
        if run["kind"] == "replay_run":
            s = read_json(run["dir"] / "summary.json") or {}
            self.summary, self.gate = s, read_json(run["dir"] / "gate.json")
            self.universe = list(s.get("symbols") or [])
            net = (s.get("costs") or {}).get("net") or {}
            fee = net.get("fee_rate", net.get("fee_taker"))
            self.fee_model = {
                "source": "repo_assumption",
                "taker_pct": None if fee is None else fee * 100,
                "maker_pct": None if net.get("fee_maker", fee) is None else net.get("fee_maker", fee) * 100,
                "slippage_bp": net.get("slippage_bps"),
                "spread_source": "measured" if s.get("broker") == "paper" else "unknown"}
            w = s.get("window") or {}
            self.replay_window = {"from_ms": adapter.iso_ms(w.get("trade_from")),
                                  "to_ms": adapter.iso_ms(w.get("to")), "speed": None}
        else:
            self.summary, self.gate, self.tail = None, None, LineTail(run["file"])
            self.fee_model = {"source": "unknown", "taker_pct": None, "maker_pct": None, "slippage_bp": None,
                              "spread_source": "unknown"}

    def _pull(self) -> list[tuple]:
        if self.run["kind"] == "scanner_once_replay":
            out = []
            for rec in self.tail.poll():
                r = adapter.scanner_signal(rec, expect_mode="REPLAY")
                if r is None or rec.get("run_id") not in (None, self.run_id):
                    self.refused += 1
                    continue
                out.append(r)
            return out
        if self.loaded:
            return []                        # a finished run's record does not change
        self.loaded = True
        row = adapter.strategy_row_from_run(self.run_id, self.summary, self.gate)
        out = [("strategy_status", None, {"items": [row]})]
        evs = self.data.replay_events(self.run)
        refs = {e["payload"].get("id"): e["payload"].get("ref_price")
                for e in evs if e.get("type") == "strategy_signal"}
        conv = adapter.ReplayEventAdapter(self.run_id, self.summary.get("broker"), refs)
        for e in evs:
            if e.get("mode") not in (None, "REPLAY"):
                self.refused += 1
                continue
            out.extend(conv.convert(e))
        return out


class LiveChannel(Channel):
    def __init__(self, data: DataRoot, run_id: str, day: str, buffer_size: int, quotes_s: float, clock=time.time,
                 shadow_root=None):
        super().__init__("LIVE_PAPER", run_id, buffer_size, clock)
        self.data, self.day, self.quotes_s = data, day, quotes_s
        self.tail = LineTail(data.signals_file(day))
        self.shadow = ShadowTail(shadow_root, day)          # P4a: shadow books' signals and fills, live
        self.gaps = GapTail(data, day)
        self.gap_marks: list[tuple[int, str | None]] = []       # (recv_ms, symbol or None for all)
        self._q: dict | None = None
        self._q_at = -1e18
        self.fee_model = {"source": "unknown", "taker_pct": None, "maker_pct": None, "slippage_bp": None,
                          "spread_source": "unknown"}

    def _pull(self) -> list[tuple]:
        out = []
        for rec in self.tail.poll():
            r = adapter.scanner_signal(rec, expect_mode="LIVE_PAPER")
            if r is None or rec.get("run_id") not in (None, self.run_id):
                self.refused += 1
                continue
            out.append(r)
        for lane, strat, rec in self.shadow.poll():
            r = adapter.shadow_record(lane, strat, rec)
            if r is not None:
                out.append(r)
        for rec in self.gaps.poll():
            dq = adapter.data_quality_from_gap(rec)
            if dq[2].get("recv_ms") is not None:
                self.gap_marks.append((dq[2]["recv_ms"], dq[2]["symbol"]))
            out.append(dq)
        return out

    def quotes(self) -> dict | None:
        """Conflated top of book for the whole universe, recomputed at most once per quotes_s."""
        now = self.clock()
        if now - self._q_at < self.quotes_s:
            return self._q
        self._q_at = now
        books = self.data.latest_by_symbol(self.day, "book")
        trades = self.data.latest_by_symbol(self.day, "trade")
        if not books and not trades:
            self._q = None
            return None
        items = []
        for sym in sorted(set(books) | set(trades)):
            b = books.get(sym)
            ref = (b or {}).get("ts_recv_ns") or (trades.get(sym) or {}).get("ts_recv_ns") or 0
            ref_ms = ref // 1_000_000
            gap_open = any(ref_ms - 60_000 <= t <= ref_ms and s in (None, sym) for t, s in self.gap_marks)
            items.append(adapter.quote_item(sym, b, trades.get(sym), gap_open))
        self.universe = [i["symbol"] for i in items]
        self._q = {"items": items}
        return self._q


class Hub:
    def __init__(self, data: DataRoot, buffer_size: int, quotes_s: float, clock=time.time, shadow_root=None):
        self.data, self.buffer_size, self.quotes_s, self.clock = data, buffer_size, quotes_s, clock
        self.shadow_root = shadow_root
        self.channels: dict[tuple[str, str], Channel] = {}
        self.clients = 0
        self.lock = threading.Lock()

    def get(self, mode: str, run_id: str, *, day: str | None = None, run: dict | None = None) -> Channel:
        key = (mode, run_id)
        with self.lock:
            ch = self.channels.get(key)
            if ch is None:
                if mode == "REPLAY":
                    ch = ReplayChannel(self.data, run_id, run, self.buffer_size, self.clock)
                else:
                    ch = LiveChannel(self.data, run_id, day, self.buffer_size, self.quotes_s, self.clock,
                                     shadow_root=self.shadow_root)
                self.channels[key] = ch
            return ch

    @property
    def dropped_total(self) -> int:
        return sum(c.dropped_total for c in self.channels.values())
