#!/usr/bin/env python3
"""token_audit_monitor.py — MONITOR-ONLY contract-risk watch for the paper universe.

STATUS: MONITOR-ONLY. This script NEVER blocks a trade and NEVER places an order.
It exists to accumulate the history that `query-token-audit` does not provide, so a
future decision about the veto can be made on evidence instead of a snapshot.

WHY IT IS NOT A VETO (measured 2026-10-01, see reports/overnight-2026-10-01.md):
  The resolver returns bridged/wrapped representations of the CEX asset, not the
  asset Binance spot actually trades. Names match legitimately ("Dogecoin",
  "Litecoin", "Aave"), so the 0.85 name-similarity guard cannot separate them, and
  the market-cap tiebreak actively PREFERS the bridged twin when it is larger:
      DOGE -> BSC  0xba2ae4… (Binance-Peg Dogecoin, $242M)   not Dogecoin
      LTC  -> BSC  0x433866… (Binance-Peg Litecoin,  $50M)   not Litecoin
      AAVE -> SOL  83kw8Rb…  (bridged Aave, $3.32B > real $2.72B on ETH)
      ETHFI-> SOL  6f2RHqH…  (bridged ether.fi) -> audited HIGH RISK, false positive
  Auditing a bridged twin can VETO a healthy CEX coin on a sibling contract's risk
  profile. So the flag is not trustworthy as a gate. Record it, do not act on it.

OUTPUT: data/token-audit-monitor.jsonl (append-only, one JSON object per run).

Usage:
  PYTHONPATH=src ./venv/bin/python scripts/token_audit_monitor.py          # full run
  PYTHONPATH=src ./venv/bin/python scripts/token_audit_monitor.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

from trading.signals import token_audit, token_resolver  # noqa: E402

OUT = BASE / "data" / "token-audit-monitor.jsonl"
TREND = BASE / "scripts" / "trend_follower.py"

# Risk level at/above which a *correctly resolved* contract would be worth
# flagging to the owner. HIGH only — MID fires on ordinary blue-chip properties
# ("Contract Upgradeable", "Mintable") and would be pure noise.
ALERT_AT = 4


def paper_universe() -> list[str]:
    src = TREND.read_text()
    m = re.search(r"SYMS = \[(.*?)\]", src, re.S)
    if not m:
        raise SystemExit("could not parse SYMS from trend_follower.py")
    return [f"{s}/USDT" for s in re.findall(r'"([A-Z0-9]+)/USDT"', m.group(1))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    syms = paper_universe()
    now = datetime.now(timezone.utc).isoformat()
    rows, alerts, unresolved = [], [], []
    t0 = time.time()

    for sym in syms:
        base = sym.split("/")[0]
        try:
            res = token_resolver.resolve(base)
        except Exception as e:                       # noqa: BLE001
            unresolved.append(base)
            continue
        if not res:
            unresolved.append(base)                  # native coin, no contract
            continue
        chain, addr, name, mcap = res
        try:
            a = token_audit.audit(str(chain), str(addr))
        except Exception as e:                       # noqa: BLE001
            rows.append({"symbol": base, "chain": str(chain),
                         "error": f"{type(e).__name__}: {e}"[:120]})
            continue
        row = {
            "symbol": base,
            "chain": str(chain),
            "resolved_name": name,
            "contract": str(addr),
            "supported": a.get("supported"),
            "has_result": a.get("has_result"),
            "risk_level": a.get("risk_level"),
            "risk_code": a.get("risk_code"),
            "hits": a.get("hits") or {},
        }
        rows.append(row)
        if (a.get("risk_code") or 0) >= ALERT_AT:
            alerts.append(row)

    rec = {
        "ts": now,
        "universe_n": len(syms),
        "resolved_n": len([r for r in rows if r.get("contract")]),
        "unresolved": unresolved,
        "alerts": alerts,
        "rows": rows,
        "elapsed_s": round(time.time() - t0, 1),
        "note": "MONITOR-ONLY — never blocks a trade. Resolution may target a "
                "bridged twin, not the CEX asset.",
    }

    if args.dry_run:
        print(json.dumps({k: v for k, v in rec.items() if k != "rows"}, indent=2))
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")

    print(f"wrote {OUT}  resolved={rec['resolved_n']}/{rec['universe_n']} "
          f"alerts={len(alerts)}  {rec['elapsed_s']}s")
    for a in alerts:
        print(f"  ⚠️  {a['symbol']} chain={a['chain']} {a['risk_level']} "
              f"{list(a['hits'].values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
