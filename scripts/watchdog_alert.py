#!/usr/bin/env python3
"""WATCHDOG — speaks only when something is wrong.

THE CONTRACT
------------
Silence means healthy.

This is the whole design. A watchdog that reports good news trains you to
ignore it, and then you ignore the bad news too. So this script:

  - exits silently (no output, no message) when everything is fine
  - emits a short alert ONLY when a check is degraded or critical
  - never repeats the same alert on every run (state is remembered)

REPEAT SUPPRESSION
------------------
A disk that hovers at 95% would otherwise page you every 15 minutes until you
mute the channel, which is how alerting dies. So each distinct problem is
announced once, then held, and only re-announced after a cool-down — or
immediately if the severity ESCALATES (warn -> crit), because that is new
information.

Usage:
  python3 watchdog_alert.py            # print alert text if any (else nothing)
  python3 watchdog_alert.py --reset    # forget suppression state
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
STATE_FILE = BASE / "state" / "watchdog-alert-state.json"

RE_ALERT_AFTER_HOURS = 12      # re-nag about an unchanged problem this often
ESCALATION_RANK = {"ok": 0, "unknown": 0, "warn": 1, "crit": 2}

sys.path.insert(0, str(BASE / "scripts"))


def fingerprint(check: dict) -> str:
    """Identity of a problem, ignoring numbers that drift.

    Deliberately uses the check NAME and its STATE, not the detail string —
    otherwise '218 MB free' -> '217 MB free' would look like a new problem and
    re-alert every single run.
    """
    return hashlib.sha256(
        f"{check['name']}:{check['state']}".encode()
    ).hexdigest()[:16]


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))
    except Exception as e:
        print(f"(could not persist watchdog state: {e})", file=sys.stderr)


def main() -> int:
    if "--reset" in sys.argv:
        STATE_FILE.unlink(missing_ok=True)
        print("watchdog state cleared")
        return 0

    import system_status as ss

    st = ss.gather()
    problems = [c for c in st["checks"] if c["state"] in ("warn", "crit")]

    if not problems:
        # Healthy: say nothing at all. Also forget old problems so a future
        # recurrence counts as fresh news.
        if STATE_FILE.exists():
            save_state({"seen": {}, "last_healthy": st["generated_at"]})
        return 0

    state = load_state()
    seen = state.get("seen", {})
    now = datetime.now(timezone.utc)

    new_alerts = []
    for c in problems:
        fp = fingerprint(c)
        prev = seen.get(fp)

        if prev is None:
            new_alerts.append(c)                 # never seen -> say it
        else:
            try:
                last = datetime.fromisoformat(prev["at"])
                age_h = (now - last).total_seconds() / 3600
            except Exception:
                age_h = 999
            escalated = ESCALATION_RANK.get(c["state"], 0) > \
                ESCALATION_RANK.get(prev.get("state", "ok"), 0)
            if escalated or age_h >= RE_ALERT_AFTER_HOURS:
                new_alerts.append(c)

        seen[fp] = {"at": now.isoformat(timespec="seconds"),
                    "state": c["state"], "name": c["name"]}

    # forget problems that have cleared, so a recurrence is announced again
    active = {fingerprint(c) for c in problems}
    seen = {k: v for k, v in seen.items() if k in active}

    save_state({"seen": seen, "last_run": st["generated_at"]})

    if not new_alerts:
        return 0      # silent: already told them, nothing has changed

    crit = any(c["state"] == "crit" for c in new_alerts)
    icon = {"warn": "⚠️", "crit": "⛔", "unknown": "❔", "ok": "✅"}

    lines = [
        "⛔ LivreSignals — CRITICAL" if crit else "⚠️ LivreSignals — degraded",
        "",
    ]
    for c in new_alerts:
        lines.append(f"{icon.get(c['state'], '?')} {c['name']}")
        lines.append(f"   {c['detail']}")
        lines.append("")

    suppressed = len(problems) - len(new_alerts)
    if suppressed > 0:
        lines.append(f"({suppressed} known issue(s) unchanged, not repeated)")

    lines.append("")
    lines.append("run: python3 /opt/trading-agent/scripts/system_status.py")

    print("\n".join(lines).strip())
    return 2 if crit else 1


if __name__ == "__main__":
    sys.exit(main())
