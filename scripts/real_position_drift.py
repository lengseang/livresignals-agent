#!/usr/bin/env python3
"""REAL POSITION DRIFT — does the trend book's ledger match the exchange?

WHY THIS EXISTS
---------------
On 2026-10-09 the trend book (REAL money, `trend_follower.py`) logged
`real positions: 3/3 | REAL=ON` at every 4-hour run, so it would not open a new
signal — the account looked fully invested.

It was not. `trend-state.json -> real.open` claimed:

    ARB/USDT   qty=34.8   entry=0.1445
    NEAR/USDT  qty=2.3    entry=2.369
    LINK/USDT  qty=0.43   entry=12.582

The exchange held NONE of them. Actual non-zero balances were dust
(ADA/BNB/SUI/XRP) plus ~$20 free USDT. LINK had in fact been sold by hand at
12.885 on 2026-10-08 (the ledger never learned) and ARB/NEAR were long gone.
So three phantom positions were consuming all three real slots, and real money
sat idle: a signal the book wanted to take could not be sized.

This is the T0.5/T0.6 defect class ("real money escaping the ledger"), in its
live form: the writer (`trend_follower.py`) is on the never-edit list, so the
job here is to DETECT the drift and say so, not to silently repair it.

WHAT IT DOES
------------
Reads `trend-state.json -> real.open` (the ledger's claim) and the exchange's
balances (the fact), and classifies each claimed position:

    MATCH    exchange holds ~qty (within one lot step)
    PARTIAL  exchange holds some, but less than claimed
    MISSING  exchange holds none of it
    EXCESS   exchange holds more than the ledger claims (an unrecorded buy)

It NEVER invents a number. A balance it cannot read is UNKNOWN, not zero. The
script makes no trades, writes no state, and imports no real-money module: it
is given a `balances()` callable, so its tests run on fixtures with no socket.

EXIT CODE
---------
0  every claimed position reconciles (or there is nothing to reconcile)
1  drift found — this is a monitor, so a found drift is a failed run

USAGE
-----
    PYTHONPATH=src python scripts/real_position_drift.py           # live read
    PYTHONPATH=src python scripts/real_position_drift.py --json    # machine
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
STATE = BASE / "data" / "trend-state.json"

# A position whose exchange balance differs from the ledger by more than this
# fraction is drift, not lot-rounding noise. 1% is comfortably above the
# rounding of the coins this book holds and well below any real partial fill.
DRIFT_TOL = 0.01

# Categorised drift, in the order we print them (worst first).
SEVERITY = {"MISSING": 0, "PARTIAL": 1, "EXCESS": 2, "MATCH": 3, "UNKNOWN": 4}


def classify(ledger_qty: float, held: object) -> str:
    """Compare a claimed quantity against what the exchange reports.

    `held` is the quantity the exchange reports, or None ONLY when the balance
    read itself failed (an UNKNOWN, never treated as 0).

    A SUCCESSFUL balance read that does not list an asset means the exchange
    holds none of it: Binance omits zero balances. That is MISSING, not UNKNOWN
    — collapsing the two would make the detector blind to the exact bug it
    exists to find (2026-10-09: ARB/NEAR/LINK absent while three REAL slots
    were consumed). The caller decides "read failed" vs "asset is absent":
    `reconcile()` takes a `balances_known` flag for precisely this reason.
    """
    if held is None:
        return "UNKNOWN"
    held = float(held)
    if ledger_qty <= 0:
        # Nothing claimed: any positive balance is an unrecorded buy.
        return "EXCESS" if held > DRIFT_TOL else "MATCH"
    if held < ledger_qty * (1 - DRIFT_TOL):
        return "MISSING" if held <= DRIFT_TOL else "PARTIAL"
    if held > ledger_qty * (1 + DRIFT_TOL):
        return "EXCESS"
    return "MATCH"


def _base(symbol: str) -> str:
    """`NEAR/USDT` -> `NEAR`; a bare `NEARUSDT` is left as the venue spells it."""
    return symbol.split("/")[0] if "/" in symbol else symbol


def load_real_open(state_path: Path | None = None) -> list:
    """The ledger's claim. A corrupt/absent file is an ERROR, not `[]`.

    Reading a broken file as "no positions" would report a clean account and
    hide the very drift this tool exists to find, so this raises instead.

    `state_path` defaults to the module-level STATE *at call time* (not as a
    bound default), so a test that patches `STATE` actually redirects the read.
    """
    path = Path(state_path) if state_path is not None else STATE
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    real = d.get("real") or {}
    return list(real.get("open") or [])


def reconcile(open_positions: list, balances: dict,
              balances_known: bool = True) -> list:
    """Pure: classify each claimed position against a {ASSET: qty} mapping.

    `balances_known=False` means the balance read itself failed, so every held
    quantity is UNKNOWN. When it is True, an asset absent from `balances` is a
    real zero (Binance omits zero balances) and classifies as MISSING.
    """
    out = []
    for p in open_positions:
        sym = p.get("symbol") or p.get("pair") or "?"
        ledger_qty = float(p.get("qty") or 0.0)
        asset = _base(sym)
        held = balances.get(asset, 0.0) if balances_known else None
        status = classify(ledger_qty, held)
        out.append({
            "symbol": sym,
            "asset": asset,
            "ledger_qty": ledger_qty,
            "held_qty": None if held is None else float(held),
            "status": status,
        })
    out.sort(key=lambda r: (SEVERITY.get(r["status"], 9), r["symbol"]))
    return out


def live_balances() -> dict:
    """Real exchange read. Only called from main(), never from a test."""
    import ccxt  # local import: keeps the module importable without network libs

    env = {}
    for line in (BASE / "config" / "live.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    ex = ccxt.binance({"apiKey": env.get("LIVE_API_KEY"),
                       "secret": env.get("LIVE_SECRET_KEY"),
                       "enableRateLimit": True})
    total = ex.fetch_balance().get("total", {})
    return {k: float(v) for k, v in total.items()
            if isinstance(v, (int, float)) and abs(v) > 1e-12}


def render(rows: list) -> str:
    lines = ["REAL POSITION DRIFT  (trend-state.json real.open vs exchange)"]
    if not rows:
        lines.append("  nothing claimed open — no drift possible")
        return "\n".join(lines)
    bad = [r for r in rows if r["status"] != "MATCH"]
    for r in rows:
        held = "UNKNOWN" if r["held_qty"] is None else f"{r['held_qty']:.6g}"
        lines.append(f"  {r['status']:<8} {r['symbol']:<11} "
                     f"ledger={r['ledger_qty']:.6g}  exchange={held}")
    lines.append(f"  -- {len(bad)} of {len(rows)} claimed position(s) do not "
                 f"reconcile")
    if bad:
        lines.append("  the ledger is wrong; the exchange is the fact. "
                     "Do NOT trust `real positions: N/3` until reconciled.")
    return "\n".join(lines)


def main(argv: list) -> int:
    as_json = "--json" in argv
    try:
        open_positions = load_real_open()
    except Exception as e:  # noqa: BLE001
        msg = f"could not read {STATE}: {type(e).__name__}: {e}"
        print(msg if as_json else f"  ⛔ {msg}", file=sys.stderr)
        return 1

    try:
        balances = live_balances()
    except Exception as e:  # noqa: BLE001
        # An unreadable exchange is UNKNOWN, not "flat": pass balances_known
        # False so every held value is UNKNOWN rather than a phantom MISSING
        # for all of them.
        print(f"  ⚠️ exchange read failed ({type(e).__name__}); balances UNKNOWN")
        rows = reconcile(open_positions, {}, balances_known=False)
    else:
        rows = reconcile(open_positions, balances, balances_known=True)

    if as_json:
        print(json.dumps({"rows": rows,
                          "drift": [r for r in rows if r["status"] != "MATCH"]},
                         indent=2))
    else:
        print(render(rows))

    return 1 if any(r["status"] != "MATCH" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
