"""Preflight for the V2 paper service: everything that must be true BEFORE the runner starts. A failed check blocks
the start (deploy/v2/run-v2.sh runs this first). Exit 0 = all required checks passed, 1 = a required check failed,
2 = usage. Output: a readable table and `<out>/preflight.json`.

It never prints the API key (only whether it is present), never places an order, never touches LIVE_ENABLED.
--offline skips the network checks (tests, air-gapped dry runs).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, Optional

MIN_FREE_GB = 2.0
MIN_PY = (3, 12)


def _check(name: str, fn: Callable[[], tuple[bool, str]], required: bool = True) -> dict:
    try:
        ok, detail = fn()
    except Exception as exc:
        ok, detail = False, f"error {type(exc).__name__}: {str(exc)[:160]}"
    return {"name": name, "ok": bool(ok), "required": required, "detail": detail}


def checks(out: Path, symbols: tuple[str, ...], offline: bool, http_get: Optional[Callable] = None) -> list[dict]:
    res = []
    res.append(_check("python>=3.12", lambda: (sys.version_info[:2] >= MIN_PY, sys.version.split()[0])))

    def deps():
        import numpy, pydantic  # noqa: E401
        return True, f"numpy {numpy.__version__}, pydantic {pydantic.__version__}"
    res.append(_check("dependencies", deps))

    def workflows():
        from trading.v2.workflow import WORKFLOW_DIR, load_position_workflow, load_workflow
        w = load_workflow(WORKFLOW_DIR / "crypto_trading_v3.json"); w.require_wire_ready()
        p = load_position_workflow(); p.require_wire_ready()
        return True, f"{w.tag}, {p.tag} wire-ready"
    res.append(_check("workflows", workflows))

    def gateway():
        from trading.v2.gateway import GatewayModel
        from trading.v2.studies.history import FEATURE_COLUMNS
        m = GatewayModel.load()
        if m.columns != FEATURE_COLUMNS:
            return False, "gateway model columns differ from features-v1"
        return True, f"{m.version}, admitted_for_entries={m.admitted} (entries capped below EXECUTE)"
    res.append(_check("gateway_model", gateway))

    def config():
        from trading.v2.approved import approved_config
        c = approved_config(symbols, 15, out)
        if c.ratchet is None or not c.context_symbols:
            return False, "approved config lacks the ratchet or the context universe"
        return True, (f"stop {c.exit_params.stop_pct:.1%} target {c.exit_params.target_pct:.1%}, "
                      f"daily cap ${c.risk_limits.daily_loss_cap_usd:g}, ratchet {c.ratchet.version}, "
                      f"context over {len(c.context_symbols)} coins")
    res.append(_check("approved_config", config))

    res.append(_check("jev_key_present", lambda: (bool(os.environ.get("TYPESAFE_API_KEY", "").strip()),
                                                   "present" if os.environ.get("TYPESAFE_API_KEY", "").strip()
                                                   else "MISSING: every decision would come from the gateway")))

    def writable():
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".preflight_write_probe"
        probe.write_text("ok"); probe.unlink()
        return True, str(out.resolve())
    res.append(_check("out_dir_writable", writable))

    def disk():
        free = shutil.disk_usage(out if out.exists() else out.parent).free / 1e9
        return free >= MIN_FREE_GB, f"{free:.1f} GB free (need {MIN_FREE_GB:g})"
    res.append(_check("disk_free", disk))

    def chains():
        from trading.shadow.chain import verify
        found = []
        for lane, strat in (("v2cycle", "pipeline"), ("v2paper", "book"), ("v2kill", "controller")):
            d = out / lane / strat
            if d.exists():
                v = verify(d)
                if not v["ok"]:
                    return False, f"{lane}/{strat} does not verify: {v['error']}"
                found.append(f"{lane}:{v['records']}")
        return True, ", ".join(found) or "no chains yet (first start)"
    res.append(_check("chains_verify", chains))

    def no_live_path():
        # the whole V2 package: no exchange client import, no order call, no LIVE_ENABLED read
        bad = []
        for f in sorted(x for x in Path(__file__).parent.rglob("*.py") if x.name != "preflight.py"):
            text = f.read_text()
            for w in ("import ccxt", "from ccxt", "create_order", "place_order", "open(\"config/LIVE_ENABLED"):
                if w in text:
                    bad.append(f"{f.name}:{w}")
        return not bad, "no order path in trading.v2" if not bad else f"found {bad}"
    res.append(_check("paper_only", no_live_path))

    if not offline:
        get = http_get or _http_get

        def binance():
            t0 = time.time()
            st, body = get("https://data-api.binance.vision/api/v3/time")
            rt = (time.time() - t0) * 1000
            if st != 200:
                return False, f"HTTP {st}"
            off = json.loads(body)["serverTime"] - int((t0 + time.time()) / 2 * 1000)
            return abs(off) < 2000, f"reachable, clock offset {off:+d} ms, rtt {rt:.0f} ms"
        res.append(_check("binance_data_api", binance))

        def typesafe():
            st, _ = get("https://api.typesafe.ai/")
            return st > 0, f"reachable (HTTP {st})"
        res.append(_check("typesafe_reachable", typesafe))
    return res


def _http_get(url: str) -> tuple[int, bytes]:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10) as r:   # noqa: S310
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def main(argv=None, http_get=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args(argv)
    syms = tuple(s.strip().upper() for s in a.symbols.split(",") if s.strip())
    res = checks(a.out, syms, a.offline, http_get)
    ok = all(r["ok"] for r in res if r["required"])
    for r in res:
        print(f"{'PASS' if r['ok'] else 'FAIL':4s}  {r['name']:20s} {r['detail']}")
    print("PREFLIGHT", "OK" if ok else "FAILED")
    try:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "preflight.json").write_text(json.dumps({"ok": ok, "checks": res, "ts": time.time()}, indent=1))
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
