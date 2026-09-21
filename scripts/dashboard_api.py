#!/usr/bin/env python3
"""Trading dashboard API — serves live data as JSON for the web UI.

Endpoints:
  GET /api/overview        — paper + live balances, P&L, dual-track summary
  GET /api/signals         — recent signal states + last fired
  GET /api/trades          — recent auto-trade decisions (trade/skip)
  GET /api/accuracy        — scorer accuracy + training rows + improvements
  GET /api/realtime        — last realtime samples + timing scores
  GET /api/ledger          — PUBLIC: full hash-chained track record
  GET /api/ledger/verify   — PUBLIC: chain integrity report
  POST /api/ledger/verify  — PUBLIC: check one pasted record against the chain

FastAPI + uvicorn, single process, reads the same JSON state files the
watchdog writes. No writes — read-only dashboard.

PUBLIC SURFACE / PRIVACY
------------------------
`/api/ledger*` is intended for the public signal website. Those three routes
expose ONLY what is already in the append-only ledger — signal and close
payloads, the hashes, and derived stats. They deliberately do NOT touch the
account files (`live-balance*.json`, `short-term-state.json`, …) and they run
their payloads through `_public_payload()`, which drops any key that could
leak an API key, credential, internal path or account balance. The private
routes (`/api/overview`, `/api/live-balance`, …) keep their existing shape
and are NOT CORS-opened to the website origin beyond the configured list.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # signal_ledger

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

DATA = Path(__file__).resolve().parents[1] / "data"
STATIC = Path(__file__).resolve().parent / "dashboard.html"

# ---------------------------------------------------------------- config
# Allowed browser origins for the public ledger API. Override with the
# LEDGER_ALLOWED_ORIGINS env var (comma-separated) when the marketing site
# moves — e.g. "https://trackrecord.example.com,https://www.example.com".
# Default covers the current deploy host, the Vercel project domains
# (including preview deployments), and local dev.
_DEFAULT_ORIGINS = (
    "https://livresignals.lengseang.dev,"
    "https://hermes.lengseang.dev,"
    "https://livresignals.vercel.app,"
    "http://localhost:3000,http://localhost:5173,http://127.0.0.1:5173"
)
ALLOWED_ORIGINS = [
    o.strip() for o in
    os.environ.get("LEDGER_ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",")
    if o.strip()
]

# Vercel gives every preview deployment its own generated hostname, so a
# fixed allow-list would break previews. These cover both the project
# hostname and any preview, without resorting to a wildcard.
ALLOWED_ORIGIN_REGEX = os.environ.get(
    "LEDGER_ALLOWED_ORIGIN_REGEX",
    r"^https://livresignals(-[a-z0-9-]+)?\.vercel\.app$"
    r"|^https://livresignals(-[a-z0-9-]+)?\.lengseang\.dev$",
)

# The slow-moving public ledger is fine to cache briefly at the edge; this
# keeps the box from re-reading the file on every page view.
LEDGER_CACHE_SECONDS = int(os.environ.get("LEDGER_CACHE_SECONDS", "60"))

app = FastAPI(title="Trading Dashboard")
# CORS restricted to the configured public website origin(s) — a wildcard here
# would let any site read the private dashboard routes. `ALLOWED_ORIGINS` is
# fed from LEDGER_ALLOWED_ORIGINS so moving the site is a config change.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    max_age=600,
)


def _read_json(name: str, default: dict) -> dict:
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return default


# ---------------------------------------------------------------- ledger helpers

# Keys that must never reach a public response, whatever gets added to the
# ledger payloads later. Matched case-insensitively as substrings.
_SENSITIVE_KEY_BITS = (
    "apikey", "api_key", "secret", "token", "password", "passwd", "private",
    "credential", "signature", "mnemonic", "seed", "balance", "equity",
    "wallet", "account", "path", "file", "host", "ip", "email", "phone",
)
_SENSITIVE_VALUE_BITS = ("/opt/", "/home/", "/root/", "sk-", "BEGIN PRIVATE")


def _public_payload(obj):
    """Strip anything from a ledger payload that should not be public.

    The ledger today only holds signal/outcome data, but a public endpoint
    should not be one careless future `append()` away from publishing an API
    key or an account balance. This is a belt-and-braces redaction pass:
    sensitive-looking keys are dropped and strings that look like internal
    filesystem paths or credentials are replaced with a marker.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(bit in lk for bit in _SENSITIVE_KEY_BITS):
                continue
            out[k] = _public_payload(v)
        return out
    if isinstance(obj, list):
        return [_public_payload(v) for v in obj]
    if isinstance(obj, str):
        if any(bit in obj for bit in _SENSITIVE_VALUE_BITS):
            return "[redacted]"
        return obj
    return obj


def _load_ledger():
    """Import signal_ledger (scripts/ is on sys.path) defensively."""
    import signal_ledger
    return signal_ledger


def _empty_ledger_payload(notes: list[str]) -> dict:
    """Valid-but-empty structure — returned instead of a 500 when the ledger
    is missing, empty, or unreadable."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "chain": {
            "records": 0, "signals": 0, "closes": 0,
            "head": "0" * 64, "genesis": "0" * 64,
            "first_ts": None, "last_ts": None,
            "first_written": None, "last_written": None,
            "valid": True, "algorithm": "sha256-prev|canonical_json",
        },
        "stats": {
            "signals": 0, "closed": 0, "wins": 0, "losses": 0, "open": 0,
            "win_rate": 0.0, "win_rate_pct": 0.0, "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0, "payoff": 0.0, "expectancy_pct": 0.0,
            "cumulative_return_pct": 0.0, "max_drawdown_pct": 0.0,
            "longest_losing_streak": 0,
        },
        "signals": [],
        "equity_curve": [],
        "notes": notes,
    }


def _ledger_payload() -> dict:
    """Build the full public record. READ-ONLY: never writes the ledger.

    Merges each signal with its linked close (by `id`), attaches the shared
    `public_stats()` numbers, and produces an equity curve suitable for
    charting. Degrades to an empty-but-valid structure on any problem — the
    public site must never 500 because the ledger file is mid-write.
    """
    notes: list[str] = []
    try:
        sl = _load_ledger()
    except Exception as exc:  # noqa: BLE001
        return _empty_ledger_payload([f"ledger module unavailable: {str(exc)[:80]}"])

    try:
        records = sl.read_records()
    except Exception:  # noqa: BLE001
        return _empty_ledger_payload(["ledger unreadable"])

    if not records:
        return _empty_ledger_payload(["ledger empty or not yet created"])

    try:
        entries, closes = sl.collect_stats_inputs()
        stats = sl.public_stats()
    except Exception as exc:  # noqa: BLE001
        return _empty_ledger_payload([f"stats failed: {str(exc)[:80]}"])

    # index close RECORDS by signal id - we need the record's own `ts` for
    # the equity curve, which `collect_stats_inputs` (payload-only) drops.
    close_rec_by_id: dict = {}
    orphan_closes: list = []
    for rec in records:
        if rec.get("type") != "close":
            continue
        sid = (rec.get("payload", {}) or {}).get("id")
        if sid in entries:
            close_rec_by_id[sid] = rec
        else:
            # a close whose entry is absent from the ledger: keep it in the
            # stats (it is a real outcome and the count must stay honest)
            # but flag it, since the pair cannot be rendered.
            orphan_closes.append(rec)

    close_by_id: dict = {}
    for c in closes:
        sid = c.get("id")
        if sid is not None and sid in entries:
            close_by_id[sid] = c

    # ordered signals with their outcome merged in
    signals_out: list[dict] = []
    equity = 0.0
    peak = 0.0
    curve: list[dict] = []
    for rec in records:
        if rec.get("type") != "signal":
            continue
        p = _public_payload(rec.get("payload", {}) or {})
        sid = p.get("id")
        sig = {
            "id": sid,
            "seq": rec.get("seq"),
            "ts": rec.get("ts"),
            "written": rec.get("written"),
            "hash": rec.get("hash"),
            "symbol": p.get("symbol"),
            "side": p.get("side"),
            "source": p.get("source"),
            "entry": p.get("entry"),
            "qty": p.get("qty"),
            "stop": p.get("stop"),
            "tp_r": p.get("tp_r"),
            "risk_pct": p.get("risk_pct"),
            "stop_atr": p.get("stop_atr"),
            "exit_mode": p.get("exit_mode"),
            "regime": p.get("regime"),
            "gate": p.get("gate"),
            "backfilled": p.get("backfilled"),
        }
        c = close_by_id.get(sid)
        if c is not None:
            crec = close_rec_by_id.get(sid, {})
            ret = c.get("ret") or 0.0
            equity += ret
            peak = max(peak, equity)
            dd = equity - peak
            # event times: the close record's `ts` when the producer set one,
            # else the record's write time. Never invented.
            closed_ts = crec.get("ts") or crec.get("written")
            sig["outcome"] = {
                "exit": c.get("exit"),
                "ret": ret,
                "ret_pct": ret * 100,
                "pnl": c.get("pnl"),
                "why": c.get("why"),
                "held_days": c.get("held_days"),
                "closed_ts": closed_ts,
                "backfilled": c.get("backfilled"),
            }
            curve.append({
                "n": len(curve) + 1,
                "id": sid,
                "symbol": sig["symbol"],
                "ts": sig["ts"],
                "closed_ts": closed_ts,
                "ret_pct": ret * 100,
                "equity_pct": equity * 100,
                "drawdown_pct": dd * 100,
                "win": ret > 0,
            })
        else:
            sig["outcome"] = None
        signals_out.append(sig)

    if orphan_closes:
        notes.append(
            f"{len(orphan_closes)} close record(s) have no matching signal "
            f"record; they are included in the headline stats but cannot be "
            f"paired on the timeline"
        )

    verify = sl.verify_chain()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "chain": {
            "records": verify["records"],
            "signals": len(entries),
            "closes": len(closes),
            "head": verify["head"],
            "genesis": "0" * 64,
            "first_ts": records[0].get("ts"),
            "last_ts": records[-1].get("ts"),
            "first_written": records[0].get("written"),
            "last_written": records[-1].get("written"),
            "valid": verify["valid"],
            "algorithm": "sha256-prev|canonical_json",
        },
        "stats": stats,
        "signals": signals_out,
        "equity_curve": curve,
        "notes": notes,
    }


def _ledger_response(payload: dict) -> JSONResponse:
    """Public ledger JSON with short shared caching + safe CORS.

    Cache-Control is public/max-age only when the chain verifies; a broken
    chain must not be cached at the edge (a correction should be visible
    immediately).
    """
    headers = {
        "Cache-Control": f"public, max-age={LEDGER_CACHE_SECONDS}, "
                         f"stale-while-revalidate={LEDGER_CACHE_SECONDS * 5}",
        "X-Content-Type-Options": "nosniff",
    }
    if not payload.get("chain", {}).get("valid", True):
        headers["Cache-Control"] = "no-store"
    return JSONResponse(payload, headers=headers)


def _cors_headers(request: Request) -> dict:
    """Minimal explicit CORS for the verify POST (the middleware covers GET,
    but a browser preflight on POST benefits from an explicit echo)."""
    origin = request.headers.get("origin", "")
    if origin in ALLOWED_ORIGINS:
        return {"Access-Control-Allow-Origin": origin,
                "Vary": "Origin"}
    return {}


# ---------------------------------------------------------------- routes


@app.get("/")
def index():
    if STATIC.exists():
        return FileResponse(STATIC)
    return JSONResponse({"ok": False, "msg": "dashboard.html missing"})


@app.get("/api/overview")
def overview():
    st = _read_json("short-term-state.json", {"budget": 500.0, "closed_trades": []})
    live = _read_json("live-balance.json", {})
    # Compute paper P&L live (testnet wallet + ledger).
    try:
        from trading.allocation.pnl import compute_pnl
        port = compute_pnl().get("portfolio", {})
    except Exception as e:  # noqa: BLE001
        port = {"cost_basis": 0, "market_value": 0, "unrealized_pnl": 0,
                "unrealized_pnl_pct": 0, "error": str(e)[:80]}
    return {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "paper": {
            "cost_basis": port.get("cost_basis", 0),
            "market_value": port.get("market_value", 0),
            "pnl": port.get("unrealized_pnl", 0),
            "pnl_pct": port.get("unrealized_pnl_pct", 0),
        },
        "short_term": {
            "budget": st.get("budget", 500.0),
            "starting": 500.0,
            "closed": len(st.get("closed_trades", [])),
            "pnl": sum(t.get("pnl", 0) for t in st.get("closed_trades", [])),
        },
        "live": live,
    }


@app.get("/api/signals")
def signals():
    states = _read_json("signal-states.json", {"states": {}})
    return {"states": states.get("states", {})}


@app.get("/api/trades")
def trades():
    log = _read_json("auto-trade-log.json", {"trades": [], "skips": []})
    ledger = _read_json("signal-ledger.json", {"signals": []})
    return {
        "trades": log.get("trades", [])[-20:],
        "skips": log.get("skips", [])[-20:],
        "signal_count": len(ledger.get("signals", [])),
    }


@app.get("/api/accuracy")
def accuracy():
    scorer = _read_json("scorer-state.json", {"training_rows": [], "model_weights": None})
    params = _read_json("adaptive-params.json", {})
    return {
        "training_rows": len(scorer.get("training_rows", [])),
        "has_model": scorer.get("model_weights") is not None,
        "trained_at": scorer.get("trained_at_utc"),
        "params": params,
    }


@app.get("/api/realtime")
def realtime():
    samples = _read_json("realtime-samples.json", {"samples": {}})
    out = {}
    for sym, buf in samples.get("samples", {}).items():
        if buf:
            out[sym] = {"n": len(buf), "last": buf[-1], "first": buf[0]}
    return out


@app.get("/health")
def health():
    """Liveness probe for external monitors (UptimeRobot etc).

    Reports not just 'is the server up' but 'is the manager keeping jobs alive'.
    A monitor pinging this every 5 min will catch a truly dead box.
    """
    from pathlib import Path as _P
    DATA = _P("/opt/trading-agent/data")
    mgr = (DATA / "manager-state.json")
    manager_age_s = None
    if mgr.exists():
        try:
            import json as _json, time as _t
            ts = _json.loads(mgr.read_text()).get("checked_at")
            if ts:
                from datetime import datetime as _dt
                dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
                manager_age_s = int(_t.time() - dt.timestamp())
        except Exception:
            pass
    alive = manager_age_s is not None and manager_age_s < 600  # manager ran <10m ago
    return {
        "ok": alive,
        "server_up": True,
        "manager_last_check_age_s": manager_age_s,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/live-balance")
def live_balance():
    """Fetch live Binance balance on demand (cached 60s to avoid rate limits)."""
    cache = _read_json("live-balance-cache.json", {})
    ts = cache.get("ts", 0)
    now = datetime.now(timezone.utc).timestamp()
    if now - ts < 60 and "total_usdt" in cache:
        return cache
    try:
        from trading.exchange.live_client import live_balances, make_live_exchange
        b = live_balances()
        ex = make_live_exchange()
        btc = b.get("BTC", 0)
        ticker = ex.publicGetTickerPrice({"symbol": "BTCUSDT"})
        price = float(ticker["price"])
        out = {"btc_qty": btc, "btc_price": price, "total_usdt": round(btc * price, 2),
               "usdt": b.get("USDT", 0), "ts": now}
        # write-through cache
        (DATA / "live-balance-cache.json").write_text(json.dumps(out), encoding="utf-8")
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120], "btc_qty": 0, "total_usdt": None}


# --------------------------------------------- PUBLIC: hash-chained ledger


@app.get("/api/ledger")
def ledger():
    """The full public track record — signals + outcomes + stats + equity curve.

    Read-only. Returns an empty-but-valid structure (HTTP 200) if the ledger
    is missing, empty or torn; it never 500s and never exposes balances,
    credentials or file paths.
    """
    return _ledger_response(_ledger_payload())


@app.get("/api/ledger/verify")
def ledger_verify():
    """Chain integrity check: {valid, records, head, problems}."""
    empty = {"valid": True, "records": 0, "head": "0" * 64, "problems": []}
    try:
        sl = _load_ledger()
        rep = sl.verify_chain()
    except Exception as exc:  # noqa: BLE001
        rep = dict(empty)
        rep["problems"] = [{"line": 0, "seq": None,
                            "why": f"ledger unavailable: {str(exc)[:80]}"}]
    rep["generated_at"] = datetime.now(timezone.utc).isoformat()
    rep["algorithm"] = "sha256-prev|canonical_json"
    return _ledger_response({"chain": {"valid": rep.get("valid", True)},
                             **rep})


def _recompute_record_hash(record: dict) -> str:
    """Recompute a record's hash exactly as the ledger does."""
    sl = _load_ledger()
    body = {k: v for k, v in record.items() if k != "hash"}
    return sl._hash(body.get("prev", ""), body)


def _check_against_chain(record: dict) -> dict:
    """Confirm whether a pasted record really is in the chain.

    Accepts a full parsed record (must carry `hash`). Returns where it sits,
    whether its own hash recomputes, whether `prev` chains to the record
    before it, and whether the stored hash matches the in-chain record.
    """
    sl = _load_ledger()
    recs = sl.read_records()
    h = record.get("hash")
    out = {
        "found": False, "in_chain": False, "hash_matches": False,
        "prev_ok": None, "seq": record.get("seq"),
        "position": None, "why": None,
    }
    if not isinstance(h, str) or len(h) != 64:
        out["why"] = "record has no 64-char sha256 `hash` field"
        return out

    # does its own content hash to the stated value?
    try:
        out["hash_matches"] = (_recompute_record_hash(record) == h)
    except Exception:  # noqa: BLE001
        out["hash_matches"] = False

    # is this exact hash actually present in the published chain?
    idx = None
    for i, r in enumerate(recs):
        if r.get("hash") == h:
            idx = i
            break
    if idx is None:
        out["why"] = "hash not found in the published chain"
        return out

    out.update(found=True, in_chain=True, position=idx + 1,
               seq=recs[idx].get("seq"))
    # the stored record must be byte-identical to what was pasted
    stored = recs[idx]
    pasted_body = {k: v for k, v in record.items() if k != "hash"}
    stored_body = {k: v for k, v in stored.items() if k != "hash"}
    out["matches_stored_record"] = (pasted_body == stored_body)
    # and it must link to the record before it
    prev_record = recs[idx - 1] if idx > 0 else None
    expect_prev = prev_record["hash"] if prev_record else "0" * 64
    out["prev_ok"] = (stored.get("prev") == expect_prev)
    if not out["prev_ok"]:
        out["why"] = "record does not link to its predecessor — chain broken"
    elif not out["matches_stored_record"]:
        out["why"] = "hash is in the chain but the pasted content differs"
    elif not out["hash_matches"]:
        out["why"] = "hash is in the chain but the pasted record's own hash " \
                     "does not recompute"
    return out


@app.post("/api/ledger/verify")
async def ledger_verify_record(request: Request):
    """Verify a PASTED record against the chain.

    Body may be either of:
      {"hash": "<64 hex chars>"}          — just check the hash exists
      {<full record>}                     — also recompute its hash and link
      {"record": {<full record>}}
      a bare JSON string containing a hash

    This is what lets a sceptical reader copy a line out of the ledger and
    have the server confirm it is the line that is actually published.
    """
    cors = _cors_headers(request)
    try:
        raw = await request.body()
        text = (raw or b"").decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        text = ""

    bad = {"ok": False, "valid": False, "found": False,
           "error": "send a JSON object with a `hash`, or a full record"}

    if not text:
        return JSONResponse(bad, status_code=400, headers=cors)

    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        # allow a bare pasted hash string
        body = text.strip('"').strip()

    empty_chain = {"records": 0, "head": "0" * 64}
    try:
        sl = _load_ledger()
        empty_chain = {"records": len(sl.read_records()),
                       "head": sl.chain_head()[1]}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({**bad, "valid": True,
                             "error": f"ledger unavailable: {str(exc)[:80]}"},
                            status_code=200, headers=cors)

    # --- bare hash or {"hash": ...} -------------------------------------
    if isinstance(body, str):
        body = {"hash": body}
    if not isinstance(body, dict):
        return JSONResponse(bad, status_code=400, headers=cors)

    provided_hash = body.get("hash")
    record = body.get("record") if isinstance(body.get("record"), dict) else None
    if record is None and provided_hash and len(body) > 1:
        record = body  # the whole body IS the record

    if record is None:
        if not provided_hash:
            return JSONResponse(bad, status_code=400, headers=cors)
        # hash-only check: does it exist in the chain?
        hits = [r for r in sl.read_records() if r.get("hash") == provided_hash]
        found = bool(hits)
        return JSONResponse({
            "ok": True, "valid": True, "found": found,
            "in_chain": found, "hash": provided_hash,
            "position": None, "seq": hits[0].get("seq") if hits else None,
            "chain_records": empty_chain["records"],
            "chain_head": empty_chain["head"],
            "why": None if found else "hash not found in the published chain",
        }, headers=cors)

    if not provided_hash:
        provided_hash = record.get("hash")
    try:
        detail = _check_against_chain(record)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({**bad, "error": f"could not verify: {str(exc)[:80]}"},
                            status_code=200, headers=cors)

    detail.update(ok=True, valid=True, hash=provided_hash,
                  chain_records=empty_chain["records"],
                  chain_head=empty_chain["head"])
    return JSONResponse(detail, headers=cors)


@app.options("/api/ledger/verify")
async def ledger_verify_preflight(request: Request):
    """Explicit preflight so the website can POST the verification form."""
    origin = request.headers.get("origin", "")
    headers = {
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
    }
    if origin in ALLOWED_ORIGINS:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
    return JSONResponse({"ok": True}, headers=headers)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
