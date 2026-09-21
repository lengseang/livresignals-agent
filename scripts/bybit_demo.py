"""Bybit DEMO REST client (direct, no ccxt) — proven working endpoint."""
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path("/opt/trading-agent")
_env = {}
for line in (BASE / "config" / "bybit-demo.env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        _env[k.strip()] = v.strip()

KEY = _env["BYBIT_DEMO_KEY"]
SEC = _env["BYBIT_DEMO_SECRET"]
DEMO = "https://api-demo.bybit.com"
PUB = "https://api.bybit.com"      # public market data (same for demo)


def _sign(ts, query, body=""):
    return hmac.new(SEC.encode(), (ts + KEY + "5000" + query + body).encode(),
                    hashlib.sha256).hexdigest()


def _req(url, path, params=None, method="GET", body=None):
    qs = urllib.parse.urlencode(params) if params else ""
    ts = str(int(time.time() * 1000))
    body_str = json.dumps(body) if body else ""
    sig = _sign(ts, qs, body_str)
    full = f"{url}{path}" + (f"?{qs}" if qs else "")
    data = body_str.encode() if body else None
    req = urllib.request.Request(full, data=data, method=method, headers={
        "X-BAPI-API-KEY": KEY, "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": "5000", "X-BAPI-SIGN": sig,
        "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())
    except Exception as e:
        return {"retCode": -1, "retMsg": str(e)[:120]}


def private(path, params=None, method="GET", body=None):
    return _req(DEMO, path, params, method, body)


def public(path, params=None):
    return _req(PUB, path, params)


if __name__ == "__main__":
    print("=== API key info ===")
    r = private("/v5/user/query-api")
    res = r.get("result", {})
    print(f"  note={res.get('note')} readOnly={res.get('readOnly')}")
    print(f"  permissions={json.dumps(res.get('permissions', {}))}")

    print("\n=== wallet balance (UNIFIED) ===")
    r = private("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    print("  ", json.dumps(r.get("result", r))[:400])

    print("\n=== positions ===")
    r = private("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    ps = [p for p in r.get("result", {}).get("list", []) if float(p.get("size") or 0) > 0]
    print(f"  open: {len(ps)}")
    for p in ps:
        print(f"    {p['symbol']} {p['side']} size={p['size']} entry={p['avgPrice']}")

    print("\n=== public tickers ===")
    for s in ["XAUUSDT", "BTCUSDT"]:
        r = public("/v5/market/tickers", {"category": "linear", "symbol": s})
        l = r.get("result", {}).get("list", [])
        if l:
            print(f"  {s}: last={l[0]['lastPrice']} vol24h=${float(l[0]['turnover24h']):,.0f}")
        else:
            print(f"  {s}: {str(r)[:100]}")
