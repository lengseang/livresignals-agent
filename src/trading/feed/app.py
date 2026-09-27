"""Read-only paper feed: GET-only REST + one SSE stream (docs/loop/designs/T6.1.md §4, docs/feed/v1.md).

Nothing here can place, modify or cancel anything: there is no write route, every non-GET method is refused
with 405 before routing, and the package imports no exchange client (tests/loop/test_feed.py import graph).
Every payload is labelled `simulated: true` and carries exactly one mode; LIVE_PAPER and REPLAY never share a
channel, a file or a response.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent

from trading.feed import adapter
from trading.feed.channel import Hub, LiveChannel
from trading.feed.sources import LIVE_ID_RE, DataRoot, component_status, read_json
from trading.replay.gate import DSR_THRESHOLD, MIN_OOS_TRADES   # imported up front: numpy/scipy are slow to load
from trading.scanner.registry import DEFAULT as REGISTRY

SERVER_VERSION = "feed-v1 (T5.1)"
ALLOWED_METHODS = {"GET", "HEAD", "OPTIONS"}
RETRY_MS = 2000
LABELS = ["SIMULATED: paper/replay only, no real capital", "research, not evidence of edge"]
Mode = Literal["LIVE_PAPER", "REPLAY"]


def _utc_day(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def create_app(data_root: Path | str, *, heartbeat_s: float = 1.0, quotes_s: float = 1.0, poll_s: float = 0.25,
               buffer_size: int = 50_000, clock=time.time, bind: str = "127.0.0.1",
               max_idle_s: float | None = None, candidates: Path | str | None = None,
               shadow: Path | str | None = None) -> FastAPI:
    """max_idle_s: close a stream after that long without sending anything (tests only; None in serve).
    candidates: research/candidates/candidates.json (ROADMAP v3 cards), served read-only by /v1/candidates."""
    data = DataRoot(data_root)
    hub = Hub(data, buffer_size, quotes_s, clock, shadow_root=shadow)
    started = clock()
    app = FastAPI(title="livresignals paper feed", version="1", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub = hub

    @app.middleware("http")
    async def read_only(request: Request, call_next):
        if request.method not in ALLOWED_METHODS:
            return JSONResponse({"detail": "read-only feed: only GET is served"}, status_code=405,
                                headers={"Allow": "GET, HEAD"})
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            # no-transform: a proxy that gzips (the Next.js /feed rewrite does) buffers the stream,
            # so the browser saw no event at all and the terminal sat on AWAITING.
            response.headers["Cache-Control"] = "no-cache, no-transform"
        return response

    # added after read_only => outermost; preflight for the local terminal only, GET only
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"], allow_methods=["GET"],
                       allow_headers=["Last-Event-ID"])

    # ---- helpers ------------------------------------------------------------------------------------
    def components() -> tuple[dict, dict]:
        day = _utc_day(clock())
        rec = component_status(data.newest_mtime(day, "book"), clock())
        comps = {"ingest": rec, "recorder": rec, "scanner": "unknown", "paper_engine": "unknown"}
        ev = {"recorder": f"freshness of ticks/{day}/book/*: ok <=10 s, degraded <=120 s, else down",
              "ingest": "same evidence as recorder (one process, trading.stream.run_ingest)",
              "scanner": "UNKNOWN: the scanner writes only when a signal fires, so a quiet file proves nothing",
              "paper_engine": "UNKNOWN: no live paper engine heartbeat exists yet"}
        return comps, ev

    def live_day_or_400(run_id: str | None) -> tuple[str, str, bool]:
        if run_id is None:
            day = _utc_day(clock())
            return f"live-{day}", day, True
        m = LIVE_ID_RE.match(run_id)
        if not m:
            raise HTTPException(400, f"LIVE_PAPER run_id must be live-YYYY-MM-DD, got {run_id!r}")
        return run_id, m.group(1), False

    def replay_run_or_error(run_id: str | None) -> dict:
        if not run_id:
            raise HTTPException(400, "REPLAY needs run_id (see /v1/runs)")
        if run_id.startswith("live-"):
            raise HTTPException(400, "a live-* run_id is LIVE_PAPER; REPLAY and LIVE_PAPER are never mixed")
        run = data.replay_run(run_id)
        if run is None:
            raise HTTPException(404, f"no REPLAY run {run_id!r}")
        return run

    def live_signal_stats(day: str) -> tuple[int, int | None]:
        n, last = 0, None
        path = data.signals_file(day)
        if path.exists():
            for rec in _read_lines(path):
                if rec.get("mode") == "LIVE_PAPER" and rec.get("simulated") is True:
                    n += 1
                    last = rec.get("ts_event_ms")
        return n, last

    # ---- REST ---------------------------------------------------------------------------------------
    @app.get("/v1/health")
    def health():
        comps, ev = components()
        return {"v": 1, "status": "ok", "simulated": True, "uptime_s": round(clock() - started, 3),
                "server_version": SERVER_VERSION, "bind": bind, "components": comps, "evidence": ev}

    @app.get("/v1/runs")
    def runs():
        items = []
        for run_id, run in data.replay_runs().items():
            if run["kind"] == "replay_run":
                s = read_json(run["dir"] / "summary.json") or {}
                g = read_json(run["dir"] / "gate.json")
                w = s.get("window") or {}
                opened = w.get("holdout_opened")
                items.append({"run_id": run_id, "mode": "REPLAY", "simulated": True, "kind": "replay_run",
                              "strategy": s.get("strategy"), "params": s.get("params"),
                              "symbols": list(s.get("symbols") or []),
                              "from_ms": adapter.iso_ms(w.get("trade_from")), "to_ms": adapter.iso_ms(w.get("to")),
                              "holdout_excluded": None if opened is None else not opened,
                              "gate_status": adapter.GATE_MAP.get((g or {}).get("verdict"), "PENDING"),
                              "unknown": [] if g else ["gate"]})
            else:
                items.append({"run_id": run_id, "mode": "REPLAY", "simulated": True, "kind": "scanner_once_replay",
                              "strategy": None, "params": None, "symbols": [], "from_ms": None, "to_ms": None,
                              "holdout_excluded": None, "gate_status": "PENDING",
                              "unknown": ["strategy", "params", "symbols", "from_ms", "to_ms", "holdout_excluded"]})
        for day in data.live_days():
            items.append({"run_id": f"live-{day}", "mode": "LIVE_PAPER", "simulated": True, "kind": "live_day",
                          "strategy": None, "params": None, "symbols": [], "from_ms": adapter.iso_ms(day),
                          "to_ms": adapter.iso_ms(day) + 86_400_000, "holdout_excluded": None,
                          "gate_status": "PENDING", "unknown": ["strategy", "params", "symbols", "holdout_excluded"]})
        return {"v": 1, "simulated": True, "items": items}

    @app.get("/v1/candidates")
    def candidates_():
        # P3: the strategy-factory league. REPLAY research, not evidence of edge; a missing file is UNKNOWN.
        doc = read_json(Path(candidates)) if candidates else None
        if not doc:
            return {"v": 1, "simulated": True, "items": [], "rejected_on_evidence": [], "unknown": ["candidates"]}
        items = []
        for c in doc.get("candidates", []):
            rules = c.get("rules") or {}
            items.append({"id": c.get("id"), "strategy": c.get("strategy"), "family": c.get("family"),
                          "stage": c.get("stage"), "claim": c.get("claim"), "prior": c.get("prior"),
                          "our_use": c.get("our_use"), "trial_budget": c.get("trial_budget"),
                          "rules_status": rules.get("status", "UNKNOWN"),
                          "deviations": list(rules.get("our_deviations") or []),
                          "sources": [{"cite": x.get("cite"), "url": x.get("url"),
                                       "peer_reviewed": x.get("peer_reviewed")} for x in c.get("sources") or []],
                          "result": c.get("result")})
        return {"v": 1, "simulated": True, "items": items,
                "rejected_on_evidence": list(doc.get("rejected_on_evidence") or []), "unknown": []}

    @app.get("/v1/shadow")
    def shadow_():
        # P3b: the shadow scanner's forward scorecards (trading.shadow.score). A missing file is UNKNOWN.
        doc = read_json(Path(shadow) / "scorecards.json") if shadow else None
        if not doc:
            return {"v": 1, "simulated": True, "books": [], "unknown": ["scorecards"]}
        keep = ("lane", "strategy", "status", "forward_days_clean", "signals", "fills", "return", "max_drawdown",
                "sharpe_daily_ann", "gap_days", "errors", "need")
        books = [{k: b.get(k) for k in keep} | {"chain_ok": (b.get("chain") or {}).get("ok")} for b in doc.get("books", [])]
        return {"v": 1, "simulated": True, "label": doc.get("label"), "books": books, "unknown": []}

    @app.get("/v1/scan")
    def scan_():
        # P4c: the shadow books' live scans. PROVISIONAL previews, never signals; latest only, one file per book.
        d = Path(shadow) / "scan" if shadow else None
        if d is None or not d.is_dir():
            return {"v": 1, "simulated": True, "provisional": True, "books": [], "unknown": ["scan"]}
        books = [b for b in (read_json(f) for f in sorted(d.glob("*.json"))) if b]
        return {"v": 1, "simulated": True, "provisional": True, "books": books, "unknown": []}

    @app.get("/v1/runs/{run_id}/summary")
    def run_summary(run_id: str):
        if LIVE_ID_RE.match(run_id):
            day = LIVE_ID_RE.match(run_id).group(1)
            if day not in data.live_days():
                raise HTTPException(404, f"no LIVE_PAPER day {run_id!r}")
            n, last = live_signal_stats(day)
            return {"v": 1, "mode": "LIVE_PAPER", "simulated": True, "run_id": run_id,
                    "summary": {"signals": n, "last_signal_ms": last}, "gate": None}
        run = replay_run_or_error(run_id)
        if run["kind"] != "replay_run":
            return {"v": 1, "mode": "REPLAY", "simulated": True, "run_id": run_id, "kind": run["kind"],
                    "summary": None, "gate": None}
        return {"v": 1, "mode": "REPLAY", "simulated": True, "run_id": run_id, "kind": "replay_run",
                "summary": read_json(run["dir"] / "summary.json"), "gate": read_json(run["dir"] / "gate.json")}

    @app.get("/v1/strategies")
    def strategies(mode: Mode, run_id: str | None = None):
        if mode == "REPLAY":
            items = []
            runs_ = [(run_id, replay_run_or_error(run_id))] if run_id else list(data.replay_runs().items())
            for rid, run in runs_:
                if run["kind"] == "replay_run":
                    items.append(adapter.strategy_row_from_run(
                        rid, read_json(run["dir"] / "summary.json") or {}, read_json(run["dir"] / "gate.json")))
        else:
            _, day, _ = live_day_or_400(run_id)
            n, last = live_signal_stats(day)
            items = [adapter.strategy_row_from_registry(
                info, None if n == 0 and not data.signals_file(day).exists() else n, last)
                for info in REGISTRY.list()]
        counts = {k: 0 for k in ("PASSED", "FAILED", "PENDING", "INSUFFICIENT")}
        for it in items:
            counts[it["gate"]["status"]] += 1
        return {"v": 1, "simulated": True, "mode": mode, "total": len(items), "counts_by_gate": counts,
                "items": items}

    # ---- SSE ----------------------------------------------------------------------------------------
    def open_channel(mode: Mode, run_id: str | None = None):
        """Runs before the response starts, so a bad mode/run_id is a plain 400/404, never a broken stream."""
        if mode == "REPLAY":
            return hub.get("REPLAY", run_id, run=replay_run_or_error(run_id)), False
        rid, day, implicit = live_day_or_400(run_id)
        return hub.get("LIVE_PAPER", rid, day=day), implicit

    @app.get("/v1/stream", response_class=EventSourceResponse)
    async def stream(request: Request, opened=Depends(open_channel),
                     topics: str | None = Query(None, description="comma list of event types; hello/resync always"),
                     max_events: int | None = Query(None, ge=1),
                     last_event_id: str | None = Header(None, alias="Last-Event-ID")):
        ch, implicit = opened
        want = None if not topics else {t.strip() for t in topics.split(",") if t.strip()}
        await anyio.to_thread.run_sync(ch.poll)
        if isinstance(ch, LiveChannel):
            await anyio.to_thread.run_sync(ch.quotes)          # fills hello.universe from the recorder
        async for ev in _events(request, ch, want, max_events, last_event_id, implicit):
            yield ev

    async def _events(request, ch, want, max_events, last_event_id, implicit):
        hub.clients += 1
        sent = 0
        try:
            lei = None
            if last_event_id is not None:
                try:
                    lei = int(last_event_id)
                except ValueError:
                    lei = None
            resync = None
            if lei is None:
                cursor = max(0, ch.oldest - 1)
            elif lei > ch.seq:
                resync, cursor = "server_restart", max(0, ch.oldest - 1)
            elif lei + 1 < ch.oldest:
                resync, cursor = "buffer_expired", max(0, ch.oldest - 1)
                ch.dropped_total += ch.oldest - lei - 1
            else:
                cursor = lei

            def ctl(typ, d, ts=None):
                return ServerSentEvent(data=ch.control(typ, d, cursor, ts), event=typ)

            hello = {"server_version": SERVER_VERSION, "schema": 1, "mode": ch.mode, "run_id": ch.run_id,
                     "badge": "SIMULATED", "universe": list(ch.universe), "fee_model": ch.fee_model,
                     "thresholds": _thresholds(), "replay": ch.replay_window,
                     "resume": {"last_event_id": lei, "from_seq": cursor + 1}, "labels": LABELS}
            yield ServerSentEvent(data=ch.control("hello", hello, cursor), event="hello", retry=RETRY_MS)
            sent += 1
            if resync:
                yield ctl("resync", {"reason": resync, "detail": _RESYNC_DETAIL[resync]})
                sent += 1
            last_hb = last_q = -1e18
            last_quotes = None
            day0 = _utc_day(clock())
            idle_since = clock()
            while max_events is None or sent < max_events:
                if max_idle_s is not None and clock() - idle_since > max_idle_s:
                    return
                if await request.is_disconnected():
                    return
                await anyio.to_thread.run_sync(ch.poll)
                evs, expired = ch.after(cursor)
                if expired:
                    cursor = max(0, ch.oldest - 1)
                    yield ctl("resync", {"reason": "buffer_expired", "detail": _RESYNC_DETAIL["buffer_expired"]})
                    sent += 1
                    continue
                for e in evs:
                    cursor = e["seq"]
                    if want is None or e["type"] in want:
                        yield ServerSentEvent(data=e, event=e["type"], id=str(e["seq"]))
                        sent += 1
                        idle_since = clock()
                        if max_events is not None and sent >= max_events:
                            return
                now = clock()
                if isinstance(ch, LiveChannel) and (want is None or "quotes" in want) and now - last_q >= hub.quotes_s:
                    last_q = now
                    q = await anyio.to_thread.run_sync(ch.quotes)
                    if q is not None and q != last_quotes:       # unchanged book: nothing to send
                        last_quotes = q
                        yield ctl("quotes", q)
                        sent += 1
                        idle_since = clock()
                        continue
                if (want is None or "heartbeat" in want) and now - last_hb >= heartbeat_s:
                    last_hb = now
                    yield ctl("heartbeat", await anyio.to_thread.run_sync(_heartbeat, ch))
                    sent += 1
                    idle_since = clock()
                    continue
                if implicit and _utc_day(now) != day0:
                    yield ctl("resync", {"reason": "run_changed", "detail": "UTC day rolled over: reconnect "
                                                                            "for the new live-<date> run"})
                    return
                await anyio.sleep(poll_s)
        finally:
            hub.clients -= 1

    def _heartbeat(ch) -> dict:
        comps, _ = components()
        clk = None
        if isinstance(ch, LiveChannel):
            c = data.last_record(ch.day, "clock")
            if c:
                recv = c.get("ts_recv_ns")
                clk = {"offset_ms": c.get("offset_ms"), "rtt_ms": c.get("rtt_ms"), "status": c.get("status"),
                       "recv_ms": None if recv is None else recv // 1_000_000}
        return {"uptime_s": round(clock() - started, 3), "seq": ch.seq, "clients": hub.clients,
                "dropped_total": hub.dropped_total, "refused_total": ch.refused, "components": comps,
                "latency_ms": {"exch_to_recv_p50": None, "exch_to_recv_p99": None, "recv_to_scan_p50": None,
                               "scan_to_emit_p50": None},
                "rates": {"ticks_per_s": None, "signals_per_min": None},
                "replay": None if ch.mode != "REPLAY" else {"sim_time_ms": None, "progress": None},
                "clock": clk,
                "unknown": ["latency_ms", "rates"] + (["replay.sim_time_ms", "replay.progress"]
                                                      if ch.mode == "REPLAY" else [])}

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def not_found(path: str):
        raise HTTPException(404, "not found")

    return app


_RESYNC_DETAIL = {
    "buffer_expired": "Last-Event-ID is older than the ring buffer: discard state; the buffered backlog follows",
    "server_restart": "Last-Event-ID is ahead of this server's seq (restart): discard state; backlog follows",
}


def _thresholds() -> dict:
    return {"min_trades": MIN_OOS_TRADES, "dsr_min": DSR_THRESHOLD}


def _read_lines(path: Path):
    import json
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
