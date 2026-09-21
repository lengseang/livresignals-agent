#!/usr/bin/env python3
"""WEEKLY DIGEST — an honest summary of what the system did this week.

WHY THIS IS SEPARATE FROM THE WATCHDOG
--------------------------------------
The watchdog is reactive: it only speaks when something breaks. This is the
opposite — a scheduled, proactive report, so that *absence of news* never
becomes *absence of information*. Without it, a quiet week and a dead week
look identical from the outside.

WHAT IT REPORTS, and why each line is here
------------------------------------------
1. What closed this week, win or lose. Not just the good ones.
2. Live vs backtest divergence. This is the honest core: the backtest is a
   claim, the live record is the test of it. If they diverge, that is the most
   important fact in the system and it must be stated plainly.
3. Backtest expectation vs reality, side by side, with the gap named.
4. New research failures. A digest that only reports successes would be
   marketing; the failure registry is part of the product's credibility.
5. Infrastructure state, so a green week is verifiable rather than asserted.

The tone is deliberately flat. No exclamation marks, no celebration. The
record is the record; a weekly report that cheers is a sales document.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "src"))


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


def main() -> int:
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    import signal_ledger as sl

    entries, closes_list = sl.collect_stats_inputs()
    stats = sl.public_stats()

    closes = {}
    for c in closes_list:
        closes[c.get("id")] = c
    recs = sl.read_records()

    # ---- this week's closes -------------------------------------------
    # Close records carry only {id, ret, exit, why, backfilled} — the symbol
    # lives in the ENTRY record, so it has to be joined in. (Getting this
    # wrong printed "?" for every symbol, which is exactly the kind of thing
    # that makes a report look broken.)
    sym_by_id = {}
    for sid, w in entries.items():
        rec = w.get("rec", {}) if isinstance(w, dict) else {}
        p = rec.get("payload", {}) if isinstance(rec, dict) else {}
        sym_by_id[sid] = w.get("symbol") or p.get("symbol") or sid

    week_closed = []
    for r in recs:
        if r.get("type") != "close":
            continue
        payload = r.get("payload") or {}
        if payload.get("backfilled"):
            continue
        ts = r.get("ts")
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            continue
        if t >= week_ago:
            week_closed.append({"rec": r, "sym": sym_by_id.get(payload.get("id"), "?"),
                                "ret": payload.get("ret"), "why": payload.get("why")})

    wins = [c for c in week_closed if (c["ret"] or 0) > 0]
    losses = [c for c in week_closed if (c["ret"] or 0) <= 0]

    # ---- divergence: live record vs backtest expectation --------------
    # champion.json stores the backtest evidence under promotions[0]; there is
    # no top-level win_rate. Read it from where it actually is.
    champ = load_json(BASE / "data" / "champion.json", {})
    promotions = champ.get("promotions") or []
    bt_wr = bt_payoff = None
    if promotions:
        top = promotions[0]
        bt_wr = top.get("wr")
        w, l = top.get("wins"), top.get("losses")
        if isinstance(w, int) and isinstance(l, int) and l:
            bt_payoff = None  # payoff needs avg win/loss, not in the record
    if bt_wr is None:
        bt_wr = champ.get("wr") or champ.get("win_rate_pct")
    bt_payoff = bt_payoff or champ.get("payoff")

    live_wr = stats.get("win_rate_pct")
    live_payoff = stats.get("payoff")
    live_n = stats.get("closed", 0)

    # ---- research registry: new failures this week --------------------
    reg = load_json(BASE / "data" / "research-registry.json", [])
    if isinstance(reg, dict):
        reg = reg.get("hypotheses", reg.get("entries", []))
    new_fails = []
    for h in reg if isinstance(reg, list) else []:
        d = h.get("date") or h.get("tested_at") or ""
        try:
            if d and datetime.fromisoformat(d.replace("Z", "+00:00")) >= week_ago:
                new_fails.append(h)
        except Exception:
            continue

    import system_status as ss
    st = ss.gather()

    # ---------------------------------------------------------------- build
    L = []
    L.append("📊 LivreSignals — weekly")
    L.append(now.strftime("%d %b %Y"))
    L.append("")

    # 1. activity
    L.append("── this week ──")
    if week_closed:
        L.append(f"closed {len(week_closed)}  ·  {len(wins)}W / {len(losses)}L")
        for c in week_closed[:8]:
            sym = (c["sym"] or "?").replace("USDT", "")
            ret = (c["ret"] or 0) * 100
            L.append(f"  {sym:<6} {ret:+6.2f}%  {c['why'] or ''}")
    else:
        L.append("no live signals closed this week")

    # 2/3. divergence — the honest core
    L.append("")
    L.append("── live vs backtest ──")
    L.append(f"live      {live_n} closed · WR {live_wr:.1f}% · payoff "
             f"{live_payoff:.2f}" if live_n else "live      no closes yet")
    if bt_wr:
        L.append(f"backtest  WR {bt_wr:.1f}%" +
                 (f" · payoff {bt_payoff:.2f}" if bt_payoff else ""))

    if live_n and bt_wr:
        gap = live_wr - bt_wr
        if live_n < 30:
            L.append(f"verdict   too early to judge ({live_n}/100 trades) — "
                     f"{gap:+.1f}pp is noise at this sample size")
        elif abs(gap) <= 10:
            L.append(f"verdict   live is tracking the backtest ({gap:+.1f}pp)")
        else:
            L.append(f"⚠️ verdict  live diverges from backtest ({gap:+.1f}pp) — "
                     f"warrants investigation")

    # 4. research honesty
    L.append("")
    L.append("── research ──")
    if new_fails:
        L.append(f"{len(new_fails)} new hypothesis/hypotheses tested and rejected")
        for h in new_fails[:4]:
            L.append(f"  · {h.get('name', h.get('id', 'unnamed'))}")
    else:
        L.append("no new hypotheses recorded this week")

    # 5. infrastructure
    L.append("")
    L.append(f"── system: {st['overall'].upper()} ──")
    for c in st["checks"]:
        if c["state"] != "ok":
            L.append(f"  {c['name']}: {c['detail']}")

    led = next((c for c in st["checks"] if c["name"] == "ledger"), None)
    if led:
        L.append(f"  ledger: {led.get('value')} — {led['detail']}")

    L.append("")
    L.append("full record: livresignals.lengseang.dev")

    print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())
