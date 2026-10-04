"""One-shot probe: send ONE real call, record the raw reply, and report whether our assumptions hold.

    TYPESAFE_API_KEY=... python -m trading.v2.jev_probe [out.json]

Run it yourself (it needs network and your key; nothing in the build/test environment ever runs it). It sends a toy
state with NO trading data, prints only the HTTP status, latency and a pass/fail translation report, and writes the
raw reply (never the key) to a file so the real shape can be checked against typesafe_client.py. A call costs a
fraction of a cent. The key is never printed or written.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from trading.v2.adapter import JevProtocolError
from trading.v2.typesafe_client import (KEY_ENV, JevTransportError, TypeSafeJevClient, UrllibTransport,
                                        build_wire_request, translate_response)
from trading.v2.workflow import WORKFLOW_DIR, load_workflow

TOY_FEATURES = {"snapshot_id": "feat_PROBE", "feature_version": "probe", "symbol": "BTCUSDT",
                "returns": {"1m": 0.0, "5m": 0.0, "15m": 0.0, "1h": 0.0, "4h": 0.0},
                "technical": {"rsi": 50.0, "atr": 0.01, "ema_fast_slope": 0.0, "ema_slow_slope": 0.0,
                              "distance_vwap": 0.0},
                "note": "synthetic probe features: not real market data"}
TOY_STATE = {"symbol": "BTCUSDT", "features": "feat_PROBE", "market": {"price": 100000.0, "spread_bps": 1.0},
             "regime": {"trend": "bullish", "volatility": "normal", "liquidity": "normal"},
             "position": {"side": "none", "size": 0, "entry_price": None, "unrealized_pnl": 0},
             "note": "synthetic probe state: not real market data"}


def probe(transport, workflow, key_present: bool, out_path: Path | None = None) -> dict:
    """Pure-ish core (transport injected) so it can be tested. Returns a report dict."""
    import os
    report: dict = {"key_present": key_present, "ok": False}
    if not key_present:
        report["error"] = f"{KEY_ENV} is not set"
        return report
    client = TypeSafeJevClient(workflow, transport)
    body = json.dumps(build_wire_request(workflow, {"state": TOY_STATE, "features": TOY_FEATURES}), separators=(",", ":")).encode()
    headers = {"Authorization": "Bearer " + os.environ[KEY_ENV].strip(), "Content-Type": "application/json"}
    t0 = time.monotonic()
    try:
        status, raw = transport.post(client.url, headers, body, 10.0)
    except JevTransportError as e:   # the call never completed: say so plainly (class name only, never text)
        report["error"] = (f"transport failure ({e}): the request did not reach the API. In a cloud sandbox this "
                           f"usually means the network policy denies the host; nothing was sent to or judged by Jev")
        return report
    report.update(status=status, latency_ms=round((time.monotonic() - t0) * 1000), bytes=len(raw))
    if out_path is not None:
        out_path.write_bytes(raw)
        report["saved_to"] = str(out_path)
    if status != 200:
        report["error"] = f"HTTP {status} (body saved; check the error code)"
        return report
    try:
        wire = json.loads(raw)
        internal = translate_response(wire, workflow)
        report.update(ok=True, model=internal["model"], answers=sorted(internal["answers"]),
                      usage=wire.get("usage"))
    except JevProtocolError as e:
        report["error"] = f"{e.category}: {e.detail}"
    except Exception as e:  # noqa: BLE001
        report["error"] = type(e).__name__
    return report


def main(argv: list[str]) -> int:
    import os
    wf = load_workflow(WORKFLOW_DIR / "crypto_trading_v2.json")
    out = Path(argv[1]) if len(argv) > 1 else Path("jev_probe_response.json")
    rep = probe(UrllibTransport(), wf, bool(os.environ.get(KEY_ENV, "").strip()), out)
    print(json.dumps(rep, indent=2, default=str))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
