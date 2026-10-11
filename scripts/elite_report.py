#!/usr/bin/env python3
"""Report every REAL spot trade the ELITE path makes. Facts only, no estimates.

Why: on 2026-10-10 the spot ELITE path was re-armed with $23.02 and is now
capable of firing. Every claim about it so far is an ESTIMATE from replay. This
script reports only what the exchange and the ledger actually say, so the
signal can be judged on real fills instead of models.

Reads:
  data/live-trades.json    the ledger the executor writes (open + closed)
  the exchange             the ground truth, to catch ledger/exchange drift
                           (the ghost-LINK bug of 2026-10-10 was exactly this)

Prints nothing but facts. If there are no new trades, it says so in one line —
the daily job stays quiet rather than mailing "no news" repeatedly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path("/opt/trading-agent")
sys.path.insert(0, str(BASE / "src"))
sys.path.insert(0, str(BASE / "scripts"))

LEDGER = BASE / "data" / "live-trades.json"
STATE = BASE / "state" / "elite-report-state.json"


def load_ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text())
    except Exception:
        return {"open": [], "closed": []}


def exchange_truth() -> dict:
    """The exchange is the truth. Never trust the ledger alone."""
    try:
        from trading.exchange.live_client import live_balances
        b = live_balances()
        return {k: float(v) for k, v in b.items() if v and float(v) > 0}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {str(e)[:80]}"}


def main() -> int:
    led = load_ledger()
    op, cl = led.get("open", []), led.get("closed", [])
    bal = exchange_truth()

    # what did we already report? only report NEW closed trades.
    # NOTE the ledger's real keys: entry, exit, open_ts_utc — not *_px.
    seen = set()
    if STATE.exists():
        try:
            seen = set(json.loads(STATE.read_text()).get("reported", []))
        except Exception:
            pass

    new = [t for t in cl if t.get("open_ts_utc", "") not in seen]

    usdt = bal.get("USDT", 0.0)
    if isinstance(usdt, str):
        usdt = 0.0
    held = {k: v for k, v in bal.items() if k != "USDT" and not k.startswith("_")}
    err = bal.get("_error")

    if not new and not op:
        # nothing to report — stay quiet, one line only
        print(f"ELITE report: no new trades. USDT {usdt:.4f}, "
              f"open {len(op)}, closed {len(cl)}."
              + (f" EXCHANGE ERROR: {err}" if err else ""))
        return 0

    out = ["ELITE spot report — real fills", "=" * 40]
    if err:
        out.append(f"!! exchange read error: {err}")
    out.append(f"wallet: USDT {usdt:.4f}   holdings: {held or 'none'}")
    out.append(f"ledger: open {len(op)}  closed {len(cl)}")
    out.append("")

    if op:
        out.append("OPEN now:")
        for t in op:
            out.append(f"  {t.get('symbol'):10} qty {t.get('qty')} "
                       f"@ {t.get('entry')}  stop {t.get('sl')}  tp {t.get('tp')}")
        out.append("")

    if new:
        out.append(f"NEW closed since last report ({len(new)}):")
        tot = 0.0
        for t in new[-20:]:
            pnl = t.get("pnl", 0) or 0
            tot += pnl
            out.append(f"  {t.get('symbol'):10} {t.get('reason','?'):6} "
                       f"pnl {pnl:+.4f}  in {t.get('entry')} out {t.get('exit')}")
        out.append(f"  net {tot:+.4f}")
        out.append("")
        # remember what we reported
        STATE.parent.mkdir(parents=True, exist_ok=True)
        seen |= {t.get("open_ts_utc", "") for t in new}
        STATE.write_text(json.dumps({"reported": sorted(seen)[-500:]}))

    # the honesty check: does the ledger agree with the exchange?
    n_open_ledger = len(op)
    n_held = len([k for k in held if k not in ("BNB", "XRP", "ADA", "SUI")])
    if n_held > n_open_ledger:
        out.append(f"!! DRIFT: ledger has {n_open_ledger} open but the exchange "
                   f"holds {n_held} non-dust assets. Check for an unrecorded buy "
                   f"(this is the ghost-LINK failure mode).")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
