"""T5.1: the read-only paper feed (trading.feed), per docs/loop/designs/T6.1.md §4 and docs/feed/.

Fixtures (tests/loop/fixtures/feed/data_local, copied to tmp_path per test so nothing mutates them):
- replay/20260926T014518Z-trend_sma: the real summary.json and gate.json of that run and the real events.jsonl
  lines with ts_ms < 2017-12-01 (a subset; nothing edited).
- replay/20260926T020511Z-trend_sma: real summary.json + events < 2018-02-01, and **no gate.json** (the gate never
  ran on it), to prove a missing gate gives PENDING/UNKNOWN, never numbers.
- ticks/2026-09-26/{book,trade}/02.jsonl: the first 40 real recorder lines; gap/01.jsonl: the real reconnect
  records; gap/02.jsonl: one hand-written aggTrade gap in the exact gaps.py shape (the real recording has none).
- signals/2026-09-26.jsonl and signals/once-replay/*.jsonl: hand-written records in the exact shape
  trading.scanner.scanner._emit + sink write (no real LIVE_PAPER signal exists yet: trend_sma needs 50 days).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from asgi_client import ASGIClient as TestClient

from trading.feed import adapter
from trading.feed.app import create_app
from trading.feed.schema import load_schemas, validate_envelope, validate_rest

REPO = Path(__file__).resolve().parents[2]
FIX = Path(__file__).parent / "fixtures" / "feed" / "data_local"
RUN_A = "20260926T014518Z-trend_sma"          # has gate.json (verdict FAIL)
RUN_B = "20260926T020511Z-trend_sma"          # no gate.json
ONCE = "20260926T030000Z-scan-trend_sma"      # scanner --once-replay output (REPLAY)
LIVE = "live-2026-09-26"
ENVELOPE_KEYS = {"v", "type", "seq", "ts_emit_ms", "ts_event_ms", "mode", "simulated", "run_id", "data"}


@pytest.fixture()
def data_root(tmp_path) -> Path:
    dst = tmp_path / "data_local"
    shutil.copytree(FIX, dst)
    return dst


@pytest.fixture()
def client(data_root):
    app = create_app(data_root, heartbeat_s=3600, quotes_s=0.0, poll_s=0.02, buffer_size=10_000, max_idle_s=1.0)
    with TestClient(app) as c:
        yield c


def read_sse(resp, n: int) -> list[dict]:
    """Parse up to n SSE events: [{'event','id','data'(parsed JSON)}]; comments (': ping') are skipped."""
    out, cur = [], {}
    for line in resp.iter_lines():
        if line == "":
            if "data" in cur:
                cur["data"] = json.loads(cur["data"])
                out.append(cur)
                if len(out) >= n:
                    break
            cur = {}
            continue
        if line.startswith(":"):
            continue
        k, _, v = line.partition(":")
        v = v[1:] if v.startswith(" ") else v
        cur[k] = (cur[k] + "\n" + v) if (k == "data" and k in cur) else v
    return out


def stream(client, n, headers=None, **params):
    params.setdefault("max_events", n)
    with client.stream("GET", "/v1/stream", params=params, headers=headers or {}) as r:
        assert r.status_code == 200, r.read()
        assert r.headers["content-type"].startswith("text/event-stream")
        return read_sse(r, n)


# ----- REST ----------------------------------------------------------------------------------------
def test_health_is_labelled_simulated(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    h = r.json()
    assert h["status"] == "ok" and h["simulated"] is True and h["v"] == 1
    assert set(h["components"]) == {"ingest", "scanner", "paper_engine", "recorder"}
    validate_rest("health", h)


def test_runs_lists_replay_runs_and_live_days_each_with_its_mode(client):
    r = client.get("/v1/runs").json()
    validate_rest("runs", r)
    by_id = {i["run_id"]: i for i in r["items"]}
    assert by_id[RUN_A]["mode"] == "REPLAY" and by_id[RUN_A]["gate_status"] == "FAILED"
    assert by_id[RUN_B]["mode"] == "REPLAY" and by_id[RUN_B]["gate_status"] == "PENDING"
    assert by_id[ONCE]["mode"] == "REPLAY" and by_id[ONCE]["kind"] == "scanner_once_replay"
    assert by_id[LIVE]["mode"] == "LIVE_PAPER"
    assert all(i["simulated"] is True for i in r["items"])


def test_run_summary_passthrough_404_and_no_path_traversal(client):
    r = client.get(f"/v1/runs/{RUN_A}/summary").json()
    assert r["mode"] == "REPLAY" and r["simulated"] is True
    assert r["summary"]["run_id"] == RUN_A and r["gate"]["verdict"] == "FAIL"
    assert client.get(f"/v1/runs/{RUN_B}/summary").json()["gate"] is None
    assert client.get("/v1/runs/nope/summary").status_code == 404
    assert client.get("/v1/runs/..%2F..%2Fconfig/summary").status_code in (400, 404)
    assert client.get("/v1/runs/..\\summary/summary").status_code in (400, 404)


def test_strategies_replay_from_gate_json(client):
    r = client.get("/v1/strategies", params={"mode": "REPLAY"}).json()
    validate_rest("strategies", r)
    rows = {i["run_id"]: i for i in r["items"]}
    a = rows[RUN_A]
    assert a["gate"]["status"] == "FAILED" and any("DSR" in x for x in a["gate"]["reasons"])
    ev = a["evidence"]
    assert ev["source"] == "REPLAY"
    assert ev["trades"]["oos"] == 76 and ev["dsr_trials"] == 4
    assert ev["dsr"] == pytest.approx(0.8898113781168717)
    # gate says p* does not apply (no fixed exits): the win rate travels with mean-net-vs-cost instead
    assert ev["win_rate_basis"] == "mean_net_vs_cost" and ev["p_star_pct"] is None
    assert ev["win_rate_pct"] == pytest.approx(25.0)
    assert ev["expectancy_net_pct"] == pytest.approx(3.352174674996061)
    assert ev["cost_rt_pct"] == pytest.approx(0.26)
    assert ev["vs_btc"]["beats"] is True
    assert ev["oos_window"]["from_ms"] == 1640995200000         # 2022-01, from gate.json (summary lacks the split)
    assert ev["is_window"] == {"from_ms": 1509494400000, "to_ms": 1640995200000}
    b = rows[RUN_B]
    assert b["gate"]["status"] == "PENDING"
    assert b["evidence"]["win_rate_pct"] is None and b["evidence"]["dsr"] is None
    assert "win_rate_pct" in b["evidence"]["unknown"]
    assert r["counts_by_gate"]["FAILED"] >= 1 and r["counts_by_gate"]["PENDING"] >= 1
    assert r["mode"] == "REPLAY" and r["simulated"] is True


def test_no_win_rate_without_p_star_or_mean_net_vs_cost(client):
    for mode in ("REPLAY", "LIVE_PAPER"):
        for row in client.get("/v1/strategies", params={"mode": mode}).json()["items"]:
            ev = row["evidence"]
            if ev["win_rate_pct"] is not None:
                ok_pstar = ev["win_rate_basis"] == "p_star" and ev["p_star_pct"] is not None
                ok_mean = (ev["win_rate_basis"] == "mean_net_vs_cost" and ev["expectancy_net_pct"] is not None
                           and ev["cost_rt_pct"] is not None)
                assert ok_pstar or ok_mean, row


def test_strategies_live_paper_from_registry_are_pending(client):
    r = client.get("/v1/strategies", params={"mode": "LIVE_PAPER"}).json()
    validate_rest("strategies", r)
    row = {i["strategy_id"]: i for i in r["items"]}["trend_sma"]
    assert row["gate"]["status"] == "PENDING" and row["evidence"]["source"] == "LIVE_PAPER"
    assert row["live"]["signals_24h"] is None or isinstance(row["live"]["signals_24h"], int)
    assert client.get("/v1/strategies", params={"mode": "REAL"}).status_code == 422


# ----- SSE -----------------------------------------------------------------------------------------
def test_replay_stream_envelopes_and_seq(client):
    evs = stream(client, 12, mode="REPLAY", run_id=RUN_A)
    assert evs[0]["event"] == "hello"
    types = [e["event"] for e in evs]
    assert "strategy_status" in types and "signal" in types and "paper_fill" in types and "position" in types
    seqs = []
    for e in evs:
        env = e["data"]
        assert set(env) == ENVELOPE_KEYS
        assert env["v"] == 1 and env["mode"] == "REPLAY" and env["simulated"] is True and env["run_id"] == RUN_A
        assert env["type"] == e["event"]
        validate_envelope(env)
        if "id" in e:                                   # replayable events carry an SSE id == seq
            assert int(e["id"]) == env["seq"]
            seqs.append(env["seq"])
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)       # strictly increasing
    assert all(evs[i]["data"]["seq"] <= evs[i + 1]["data"]["seq"] for i in range(len(evs) - 1))


def test_replay_events_are_time_ordered_and_keep_every_source_field(client):
    evs = [e["data"] for e in stream(client, 80, mode="REPLAY", run_id=RUN_A, topics="signal,paper_fill,position")
           if "id" in e]                                    # replayable events only (hello has no market time)
    ts = [e["ts_event_ms"] for e in evs]
    assert ts == sorted(ts)
    fills = [e["data"] for e in evs if e["type"] == "paper_fill"]
    assert {f["account"] for f in fills} >= {"net", "gross", "stress"}
    close = next(f for f in fills if f["intent"] == "CLOSE")
    assert {"ret", "pnl", "fee_total", "held_days", "closed_ts", "why"} <= set(close["extra"])


def test_resume_with_last_event_id(client):
    first = stream(client, 8, mode="REPLAY", run_id=RUN_A, topics="signal,paper_fill,position,strategy_status")
    ids = [e for e in first if "id" in e]
    k = ids[3]
    again = stream(client, 3, headers={"Last-Event-ID": k["id"]}, mode="REPLAY", run_id=RUN_A,
                   topics="signal,paper_fill,position,strategy_status")
    assert again[0]["event"] == "hello"
    resumed = [e for e in again if "id" in e]
    assert resumed[0]["data"]["seq"] == ids[4]["data"]["seq"]
    assert resumed[0]["data"]["data"] == ids[4]["data"]["data"]


def test_resume_outside_buffer_sends_resync(data_root):
    app = create_app(data_root, heartbeat_s=3600, quotes_s=0.0, poll_s=0.02, buffer_size=5, max_idle_s=1.0)
    with TestClient(app) as c:
        evs = stream(c, 3, headers={"Last-Event-ID": "1"}, mode="REPLAY", run_id=RUN_A)
        assert [e["event"] for e in evs[:2]] == ["hello", "resync"]
        assert evs[1]["data"]["data"]["reason"] == "buffer_expired"
        evs = stream(c, 2, headers={"Last-Event-ID": "999999"}, mode="REPLAY", run_id=RUN_A)
        assert evs[1]["event"] == "resync" and evs[1]["data"]["data"]["reason"] == "server_restart"


def test_mode_separation(client):
    live = stream(client, 30, mode="LIVE_PAPER", run_id=LIVE)
    assert live and all(e["data"]["mode"] == "LIVE_PAPER" and e["data"]["run_id"] == LIVE for e in live)
    sigs = [e["data"] for e in live if e["event"] == "signal"]
    assert [s["data"]["signal_id"] for s in sigs] == ["trend_sma:SOLUSDT:1790387940000",
                                                      "trend_sma:BTCUSDT:1790388000000"]
    once = stream(client, 5, mode="REPLAY", run_id=ONCE)
    assert all(e["data"]["mode"] == "REPLAY" for e in once)
    assert [e["data"]["data"]["signal_id"] for e in once if e["event"] == "signal"] == \
        ["trend_sma:SOLUSDT:1790387940000"]
    # a replay run id on the live mode, or a live id on replay, is refused, never served mixed
    assert client.get("/v1/stream", params={"mode": "LIVE_PAPER", "run_id": RUN_A}).status_code == 400
    assert client.get("/v1/stream", params={"mode": "REPLAY", "run_id": LIVE}).status_code == 400
    assert client.get("/v1/stream", params={"mode": "REPLAY"}).status_code == 400
    assert client.get("/v1/stream", params={"mode": "REPLAY", "run_id": "nope"}).status_code == 404


def test_live_signals_file_is_tailed(data_root):
    # its own app: the stream must outlive the append even on a loaded machine (it ends on max_events)
    app = create_app(data_root, heartbeat_s=3600, quotes_s=0.0, poll_s=0.02, max_idle_s=30.0)
    with TestClient(app) as client:
        _tail(data_root, client)


def _tail(data_root, client):
    path = data_root / "signals" / "2026-09-26.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["seq"] = 3
    rec["data"]["signal_id"] = "trend_sma:BTCUSDT:1790388060000"
    rec["ts_event_ms"] = 1790388060000
    got = []
    with client.stream("GET", "/v1/stream", params={"mode": "LIVE_PAPER", "run_id": LIVE, "topics": "signal",
                                                    "max_events": 4}) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                got.append(json.loads(line[5:]))
                if len(got) == 3:           # hello + the 2 existing signals: now append a new line
                    with open(path, "a", encoding="utf-8", newline="\n") as f:
                        f.write(json.dumps(rec) + "\n")
                if len(got) == 4:
                    break
    assert got[-1]["data"]["signal_id"] == "trend_sma:BTCUSDT:1790388060000"


def test_signal_adapter_reconciles_scanner_schema(data_root):
    rec = json.loads((data_root / "signals" / "2026-09-26.jsonl").read_text(encoding="utf-8").splitlines()[0])
    typ, ts, d = adapter.scanner_signal(rec)
    assert typ == "signal" and ts == rec["ts_event_ms"]
    assert d["class"] is None and d["cost_rt_pct"] is None and d["p_star"] is None
    assert {"class", "cost_rt_pct", "p_star", "tp_pct", "sl_pct", "expected_move_pct"} <= set(d["unknown"])
    for k in ("action", "interval", "params", "family", "evidence"):          # scanner extras are kept
        assert d[k] == rec["data"][k]
    assert d["source"] == {"kind": "scanner", "seq": 1, "run_id": LIVE, "ts_emit_ms": rec["ts_emit_ms"]}
    # every scanner data key survives (mapped or under extra): nothing is silently dropped
    assert set(rec["data"]) <= set(d) | set(d["extra"])
    bad = {**rec, "simulated": False}
    assert adapter.scanner_signal(bad) is None


def _last(path: Path, symbol: str) -> dict:
    return [r for r in map(json.loads, path.read_text(encoding="utf-8").splitlines()) if r["symbol"] == symbol][-1]


def test_quotes_and_data_quality_from_recorder(client, data_root):
    book = _last(data_root / "ticks/2026-09-26/book/02.jsonl", "SOLUSDT")["data"]
    trade = _last(data_root / "ticks/2026-09-26/trade/02.jsonl", "SOLUSDT")["data"]
    evs = stream(client, 6, mode="LIVE_PAPER", run_id=LIVE, topics="quotes,data_quality")
    q = next(e["data"] for e in evs if e["event"] == "quotes")
    validate_envelope(q)
    items = {i["symbol"]: i for i in q["data"]["items"]}
    sol = items["SOLUSDT"]
    assert sol["bid"] == book["bid"] and sol["ask"] == book["ask"]          # latest line wins
    assert sol["spread_bp"] == pytest.approx((book["ask"] - book["bid"]) / ((book["ask"] + book["bid"]) / 2) * 1e4)
    assert sol["last"] == trade["price"] and sol["trade_ms"] == trade["trade_ms"]
    assert q["ts_event_ms"] is None                    # book quotes carry no exchange time
    dq = [e["data"]["data"] for e in evs if e["event"] == "data_quality"]
    kinds = [d["kind"] for d in dq]
    assert kinds.count("RECONNECT") == 3 and kinds.count("GAP") == 1
    gap = next(d for d in dq if d["kind"] == "GAP")
    assert gap["gap"] == {"from_id": 4074235377, "to_id": 4074235379, "missing": 3} and gap["symbol"] == "BTCUSDT"


def test_heartbeat(data_root):
    app = create_app(data_root, heartbeat_s=0.0, quotes_s=3600, poll_s=0.02, max_idle_s=1.0)
    with TestClient(app) as c:
        evs = stream(c, 3, mode="REPLAY", run_id=RUN_B, topics="heartbeat")
        hb = next(e for e in evs if e["event"] == "heartbeat")
        assert "id" not in hb                          # control events never move Last-Event-ID
        validate_envelope(hb["data"])
        assert hb["data"]["data"]["seq"] >= 1


def test_every_fixture_event_validates_against_published_schema(client):
    schemas = load_schemas()
    assert {"envelope", "hello", "heartbeat", "quotes", "signal", "paper_fill", "position", "strategy_status",
            "data_quality", "resync", "health", "runs", "strategies"} <= set(schemas)
    n = 0
    for mode, run in (("REPLAY", RUN_A), ("REPLAY", RUN_B), ("REPLAY", ONCE), ("LIVE_PAPER", LIVE)):
        for e in stream(client, 200, mode=mode, run_id=run, max_events=60):
            validate_envelope(e["data"])
            n += 1
    assert n > 60


def test_schema_rejects_dishonest_payloads(client):
    env = stream(client, 2, mode="REPLAY", run_id=RUN_A, topics="signal")[1]["data"]
    for bad in ({**env, "simulated": False}, {**env, "v": 2}, {**env, "mode": "LIVE"}):
        with pytest.raises(ValueError):
            validate_envelope(bad)


# ----- read-only, bind, import graph ----------------------------------------------------------------
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/v1/health", "/v1/runs", f"/v1/runs/{RUN_A}/summary", "/v1/strategies",
                                  "/v1/stream", "/v1/orders", "/"])
def test_non_get_is_refused(client, method, path):
    r = client.request(method, path)
    assert r.status_code == 405


def test_only_get_routes_exist(data_root):
    app = create_app(data_root)
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods:
            assert methods <= {"GET", "HEAD"}, (route.path, methods)


def test_serve_binds_loopback_only():
    from trading.feed import serve
    assert serve.parse_args([]).host == "127.0.0.1"
    assert serve.parse_args(["--port", "8765"]).port == 8765
    for bad in ("0.0.0.0", "::", "192.168.1.5"):
        with pytest.raises(SystemExit):
            serve.parse_args(["--host", bad])


def test_feed_package_never_imports_exchange_or_real_money_modules():
    code = (
        "import sys, trading.feed, trading.feed.app, trading.feed.adapter, trading.feed.sources, "
        "trading.feed.channel, trading.feed.schema, trading.feed.serve;"
        "bad=[m for m in sys.modules if m=='ccxt' or m.startswith('ccxt.') or m.startswith('trading.exchange')"
        " or m in ('trend_follower','live_executor','futures_executor','lev_satellite_manage','unified_trader',"
        "'manager')];"
        "print(','.join(bad))"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == ""


def test_stream_forbids_proxy_transforms(client):
    # a gzipping proxy (the Next.js /feed rewrite) buffers an SSE stream unless it says no-transform
    with client.stream("GET", "/v1/stream", params={"mode": "REPLAY", "run_id": RUN_A, "max_events": 1}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "no-transform" in r.headers["cache-control"]


def test_candidates_are_served_read_only_and_validate(data_root, tmp_path):
    cards = {"candidates": [{"id": "A1-x", "strategy": "donchian_ensemble", "family": "trend", "stage": "rejected",
                             "claim": "c", "prior": "p", "our_use": "u", "trial_budget": 1,
                             "rules": {"status": "KNOWN", "our_deviations": ["d1"]},
                             "sources": [{"cite": "s", "url": "https://x", "peer_reviewed": False}],
                             "result": {"verdict": "FAIL", "metrics": {"oos_sharpe_ann": 0.77}}}],
             "rejected_on_evidence": [{"id": "X1", "why": "w"}]}
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps(cards), encoding="utf-8")
    c = TestClient(create_app(data_root, heartbeat_s=3600, quotes_s=0.0, max_idle_s=1.0, candidates=path))
    body = c.get("/v1/candidates").json()
    validate_rest("candidates", body)
    assert body["items"][0]["result"]["metrics"]["oos_sharpe_ann"] == 0.77
    assert body["items"][0]["deviations"] == ["d1"] and body["unknown"] == []


def test_missing_candidates_file_is_unknown_not_empty_success(data_root, tmp_path):
    c = TestClient(create_app(data_root, heartbeat_s=3600, quotes_s=0.0, max_idle_s=1.0,
                              candidates=tmp_path / "nope.json"))
    body = c.get("/v1/candidates").json()
    validate_rest("candidates", body)
    assert body["items"] == [] and body["unknown"] == ["candidates"]


def test_shadow_scorecards_are_served_and_validate(data_root, tmp_path):
    (tmp_path / "scorecards.json").write_text(json.dumps({"label": "SHADOW", "books": [
        {"lane": "rejected", "strategy": "donchian_ensemble", "status": "ACCUMULATING", "forward_days_clean": 3,
         "signals": 2, "chain": {"ok": True}}]}), encoding="utf-8")
    c = TestClient(create_app(data_root, heartbeat_s=3600, quotes_s=0.0, max_idle_s=1.0, shadow=tmp_path))
    body = c.get("/v1/shadow").json()
    validate_rest("shadow", body)
    assert body["books"][0]["status"] == "ACCUMULATING" and body["books"][0]["chain_ok"] is True


def test_missing_scorecards_are_unknown(data_root, tmp_path):
    c = TestClient(create_app(data_root, heartbeat_s=3600, quotes_s=0.0, max_idle_s=1.0, shadow=tmp_path / "none"))
    body = c.get("/v1/shadow").json()
    validate_rest("shadow", body)
    assert body["books"] == [] and body["unknown"] == ["scorecards"]


def test_p4a_shadow_signals_fills_and_gaps_stream_live(data_root, tmp_path):
    from trading.shadow.chain import ShadowChain
    day_ms = adapter.iso_ms("2026-09-26")
    ch = ShadowChain(tmp_path / "shadow", "rejected", "donchian_ensemble", day_ms)
    ch.write({"v": 1, "type": "signal", "ts_emit_ms": day_ms + 10, "ts_event_ms": day_ms + 5, "mode": "LIVE_PAPER",
              "simulated": True, "data": {"signal_id": "donchian_ensemble:ETHUSDT:1", "strategy_id": "donchian_ensemble",
                                          "symbol": "ETHUSDT", "side": "LONG", "action": "target", "interval": "1d",
                                          "ref_price": 4000.0, "reason": "ensemble x vol scale -> weight 0.05", "weight": 0.05,
                                          # the rest exactly as trading.scanner.scanner._emit writes them
                                          "strategy_version": "1", "family": "trend", "params": {},
                                          "class": None, "tp_pct": None, "sl_pct": None, "expected_move_pct": None,
                                          "cost_rt_pct": None, "p_star": None, "gate_status_at_fire": "PENDING",
                                          "evidence": "research/candidates/candidates.json#A1-donchian-ensemble",
                                          "latency_ms": {"tick_to_signal": 12.5}}})
    ch.write({"v": 1, "type": "shadow_fill", "ts_event_ms": day_ms + 60_000, "mode": "LIVE_PAPER", "simulated": True,
              "data": {"fill_type": "rebalance", "id": "donchian_ensemble:ETHUSDT:1", "symbol": "ETHUSDT",
                       "qty": 0.0125, "price": 4001.0, "fee": 0.05, "account": "rejected:donchian_ensemble"}})
    ch.write({"v": 1, "type": "gap", "ts_event_ms": day_ms + 120_000, "mode": "LIVE_PAPER", "simulated": True,
              "data": {"from_ms": day_ms + 100_000, "to_ms": day_ms + 120_000, "reason": "stream reconnect"}})
    c = TestClient(create_app(data_root, heartbeat_s=3600, quotes_s=0.0, poll_s=0.02, max_idle_s=1.0,
                              shadow=tmp_path / "shadow"))
    evs = stream(c, 40, mode="LIVE_PAPER", run_id=LIVE)
    shadow = [e for e in evs if (e["data"]["data"].get("extra") or {}).get("lane") == "rejected"]
    kinds = {e["event"] for e in shadow}
    assert {"signal", "paper_fill", "data_quality"} <= kinds
    for e in shadow:
        validate_envelope(e["data"])
    sig = next(e for e in shadow if e["event"] == "signal")["data"]["data"]
    assert sig["source"]["kind"] == "shadow" and sig["symbol"] == "ETHUSDT" and sig["action"] == "target"
    assert sig["extra"]["weight"] == 0.05
    fill = next(e for e in shadow if e["event"] == "paper_fill")["data"]["data"]
    assert fill["intent"] == "OPEN" and fill["side"] == "BUY" and fill["fill_price"] == 4001.0
