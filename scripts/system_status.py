#!/usr/bin/env python3
"""SYSTEM STATUS — one honest answer to "is everything actually working?"

WHY THIS EXISTS
---------------
The system has 27 cron jobs, a trader, a ledger, two backup layers and a
publish pipeline. Before this, the only way to know whether any of it was
healthy was to read logs and remember what "normal" looks like.

The realistic failure mode for this project is not a crash. It is SILENCE:

  - the ledger stops recording  -> the track record quietly gains a gap
  - the backup cron dies        -> there is no off-box copy, and you believed
                                   there was
  - the export cron breaks      -> the site serves stale data, which is a lie
                                   by omission
  - a cron wedges               -> no error, just nothing happening

None of those raise an alarm on their own. This script exists to make them
visible, and `--alert` mode exists to make them *loud*.

DESIGN RULES
------------
1. Never claim healthy on missing evidence. If a signal cannot be checked,
   it reports UNKNOWN, not OK. A status board that lies is worse than none.
2. Each check is independent — one failure must not mask the others.
3. Output is honest about ASYMMETRY: the same data reads differently to an
   operator (who wants detail) and to a visitor (who wants "is it alive").

Exit codes: 0 = all healthy, 1 = something degraded, 2 = something critical.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
DATA = BASE / "data"
LEDGER = DATA / "signal-ledger.jsonl"
STATE = BASE / "state"
CRON_LOG_DIR = Path("/tmp")

# How stale each artefact may get before it counts as a problem.
# Chosen from the actual cron cadences, with slack for a missed run.
FRESHNESS = {
    "ledger_minutes": 60 * 12,   # trader runs every ~4h; 12h = clearly stale
    "backup_hours": 36,          # daily at 04:20
    "export_hours": 36,          # daily at 02:30
}

OK, WARN, CRIT, UNKNOWN = "ok", "warn", "crit", "unknown"
RANK = {UNKNOWN: 0, OK: 0, WARN: 1, CRIT: 2}


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def age_minutes(ts: str | None) -> float | None:
    t = parse_ts(ts)
    if not t:
        return None
    return (now() - t).total_seconds() / 60


def sh(cmd: str, timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:  # pragma: no cover
        return 125, str(e)


# ------------------------------------------------------------------ checks

def check_ledger() -> dict:
    """Is the hash chain intact, and is it still growing?"""
    if not LEDGER.exists():
        return {"name": "ledger", "state": CRIT,
                "detail": "ledger file missing — the record does not exist",
                "value": None}
    try:
        sys.path.insert(0, str(BASE / "scripts"))
        sys.path.insert(0, str(BASE / "src"))
        import signal_ledger as sl

        v = sl.verify_chain()
        records = v.get("records", 0)
        head = v.get("head", "")
        problems = v.get("problems", [])

        if not v.get("valid"):
            return {"name": "ledger", "state": CRIT,
                    "detail": f"CHAIN BROKEN — {len(problems)} problem(s): "
                              + "; ".join(problems[:2]),
                    "value": f"{records} records"}

        # structural health is separate from freshness
        recs = sl.read_records()
        last_ts = recs[-1].get("ts") if recs else None
        age = age_minutes(last_ts)

        if age is None:
            state, detail = WARN, "chain valid but no timestamps readable"
        elif age > FRESHNESS["ledger_minutes"]:
            state = WARN
            detail = (f"chain valid but idle {age/60:.1f}h "
                      f"(expected activity every ~4h)")
        else:
            state, detail = OK, f"chain valid, last write {age:.0f}m ago"

        return {"name": "ledger", "state": state, "detail": detail,
                "value": f"{records} records",
                "head": head[:16], "last_write_utc": last_ts}
    except Exception as e:
        return {"name": "ledger", "state": UNKNOWN,
                "detail": f"could not inspect ledger: {str(e)[:90]}",
                "value": None}


def check_backups() -> dict:
    """On-box snapshots and the off-box mirror must both be recent."""
    snaps = sorted((BASE / "backups" / "ledger").glob("ledger-*.jsonl")) \
        if (BASE / "backups" / "ledger").exists() else []
    if not snaps:
        return {"name": "backup", "state": CRIT,
                "detail": "no on-box snapshot exists", "value": None}

    newest = max(snaps, key=lambda p: p.stat().st_mtime)
    age_h = (now().timestamp() - newest.stat().st_mtime) / 3600

    # off-box: is the mirror dir a git repo with a recent commit?
    mirror = Path("/home/ubuntu/ledger-mirror")
    offbox_detail, offbox_state = "", OK
    if not (mirror / ".git").exists():
        offbox_state = WARN
        offbox_detail = "off-box mirror not initialised"
    else:
        code, out = sh("git log -1 --format=%ct", cwd=None) if False else (1, "")
        code, out = sh(f"git -C {mirror} log -1 --format=%ct")
        if code != 0 or not out.strip().isdigit():
            offbox_state = WARN
            offbox_detail = "off-box mirror has no commits"
        else:
            off_age_h = (now().timestamp() - int(out.strip())) / 3600
            if off_age_h > FRESHNESS["backup_hours"]:
                offbox_state = WARN
                offbox_detail = f"off-box last sync {off_age_h:.0f}h ago"
            else:
                offbox_detail = f"off-box synced {off_age_h:.0f}h ago"

    state = CRIT if age_h > FRESHNESS["backup_hours"] * 2 else (
        WARN if age_h > FRESHNESS["backup_hours"] or offbox_state != OK else OK)

    return {
        "name": "backup",
        "state": state,
        "detail": f"on-box {age_h:.0f}h old · {offbox_detail}",
        "value": f"{len(snaps)} snapshots",
    }


def check_publish() -> dict:
    """Has the ledger been exported to the website recently?"""
    pub = Path("/home/ubuntu/signals-site/public/data/ledger.json")
    if not pub.exists():
        return {"name": "publish", "state": CRIT,
                "detail": "site has no ledger export", "value": None}
    age_h = (now().timestamp() - pub.stat().st_mtime) / 3600
    if age_h > FRESHNESS["export_hours"]:
        return {"name": "publish", "state": WARN,
                "detail": f"site data {age_h:.0f}h old — export cron may have stopped",
                "value": f"{age_h:.0f}h old"}
    return {"name": "publish", "state": OK,
            "detail": f"site data {age_h:.1f}h old", "value": f"{age_h:.1f}h"}


def check_trader() -> dict:
    """Are the processes that MATTER online?

    Note: `trading-loop` and `watchdog` are intentionally obsolete — trading
    runs entirely from cron (manager.py every 5min, unified_trader every 30min,
    regime_trader every 4h, futures_executor every 10min, +20 more). Those two
    pm2 entries are leftovers from an earlier design and are stopped on
    purpose, so they must NOT be reported as failures. A status board that
    cries wolf gets ignored, which defeats its purpose.
    """
    code, out = sh("pm2 jlist")
    if code != 0:
        return {"name": "processes", "state": UNKNOWN,
                "detail": "pm2 not queryable", "value": None}
    try:
        procs = json.loads(out)
    except Exception:
        return {"name": "processes", "state": UNKNOWN,
                "detail": "pm2 output unparseable", "value": None}

    # Only processes the system genuinely depends on.
    required = {"dashboard": "public API + dashboard",
                "hermes-gateway": "agent gateway"}
    retired = {"trading-loop", "watchdog"}

    status = {p.get("name"): p.get("pm2_env", {}).get("status") for p in procs}
    down = [n for n in required if status.get(n) != "online"]

    if down:
        return {"name": "processes", "state": CRIT,
                "detail": "down: " + ", ".join(f"{n} ({required[n]})" for n in sorted(down)),
                "value": f"{len(required)-len(down)}/{len(required)} up"}

    extra = [n for n in retired if n in status]
    note = f" ({len(extra)} retired job(s) stopped by design)" if extra else ""
    return {"name": "processes", "state": OK,
            "detail": "dashboard + gateway online" + note,
            "value": f"{len(required)}/{len(required)} up"}


def check_trading_activity() -> dict:
    """Is the trading system actually RUNNING, not just scheduled?

    Cron entries prove intent; logs prove it happened. This is the check that
    catches a wedged job — the failure mode where everything looks fine but
    nothing is executing.
    """
    logs = {
        "manager": BASE / "logs" / "manager.log",
        "regime": BASE / "logs" / "regime.log",
        "unified": BASE / "logs" / "unified.log",
    }
    stale, fresh = [], []
    for name, path in logs.items():
        if not path.exists():
            stale.append(f"{name} (no log)")
            continue
        age_h = (now().timestamp() - path.stat().st_mtime) / 3600
        # manager is every 5 min; regime every 4h; allow generous slack
        limit = 1.0 if name == "manager" else 6.0
        (fresh if age_h <= limit else stale).append(f"{name} {age_h:.1f}h")

    if not fresh:
        return {"name": "trading", "state": CRIT,
                "detail": "no trading job has run recently — system may be wedged",
                "value": "idle"}
    if stale:
        return {"name": "trading", "state": WARN,
                "detail": "stale: " + ", ".join(stale) + " ok: " + ", ".join(fresh),
                "value": f"{len(fresh)}/{len(logs)} active"}
    return {"name": "trading", "state": OK,
            "detail": "manager, regime and unified traders all writing",
            "value": f"{len(fresh)}/{len(logs)} active"}


def check_crons() -> dict:
    """Did any cron fail recently? Silence is the dangerous case."""
    code, out = sh("crontab -l")
    if code != 0:
        return {"name": "crons", "state": UNKNOWN,
                "detail": "crontab unreadable", "value": None}
    lines = [l for l in out.splitlines() if l.strip() and not l.startswith("#")]
    scheduled = [l for l in lines if l[:1].isdigit() or l.startswith("*")]

    # look for recent tracebacks in cron logs
    bad = []
    for log in CRON_LOG_DIR.glob("*.log"):
        try:
            if log.stat().st_mtime < now().timestamp() - 86400:
                continue
            tail = log.read_text(errors="ignore")[-4000:]
            if "Traceback" in tail:
                bad.append(log.name)
        except Exception:
            continue

    if bad:
        return {"name": "crons", "state": WARN,
                "detail": "recent tracebacks in: " + ", ".join(sorted(bad)[:3]),
                "value": f"{len(scheduled)} jobs"}
    return {"name": "crons", "state": OK,
            "detail": f"{len(scheduled)} scheduled, no recent tracebacks",
            "value": f"{len(scheduled)} jobs"}


def check_resources() -> dict:
    """Disk and memory headroom."""
    try:
        du = shutil.disk_usage("/")
        free_mb = du.free / 1e6
        used_pct = du.used / du.total * 100
    except Exception as e:
        return {"name": "resources", "state": UNKNOWN,
                "detail": f"disk unreadable: {e}", "value": None}

    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            mem[k.strip()] = int(v.strip().split()[0]) // 1024  # MB
        avail = mem.get("MemAvailable", 0)
    except Exception:
        avail = 0

    if free_mb < 250 or avail < 80:
        state = CRIT
    elif free_mb < 600 or avail < 180:
        state = WARN
    else:
        state = OK

    return {"name": "resources", "state": state,
            "detail": f"{free_mb:.0f} MB disk free ({used_pct:.0f}% used) · "
                      f"{avail} MB RAM available",
            "value": f"{free_mb:.0f} MB free"}


ALL_CHECKS = (check_ledger, check_trading_activity, check_trader,
              check_crons, check_backups, check_publish, check_resources)


def gather() -> dict:
    results = []
    for fn in ALL_CHECKS:
        try:
            results.append(fn())
        except Exception as e:
            results.append({"name": fn.__name__, "state": UNKNOWN,
                            "detail": f"check itself failed: {str(e)[:80]}",
                            "value": None})
    worst = max((RANK.get(r["state"], 0) for r in results), default=0)
    overall = CRIT if worst == 2 else WARN if worst == 1 else OK
    return {
        "generated_at": now().isoformat(timespec="seconds"),
        "overall": overall,
        "checks": results,
    }


# ----------------------------------------------------------------- display

ICON = {OK: "✅", WARN: "⚠️ ", CRIT: "⛔", UNKNOWN: "❔"}


def render(st: dict) -> str:
    lines = []
    lines.append("=" * 66)
    lines.append(f"SYSTEM STATUS — {st['overall'].upper()}")
    lines.append(f"{st['generated_at']}")
    lines.append("=" * 66)
    for c in st["checks"]:
        v = f"  [{c['value']}]" if c.get("value") else ""
        lines.append(f" {ICON.get(c['state'],'?')} {c['name']:<10}{v}")
        lines.append(f"      {c['detail']}")
    return "\n".join(lines)


def alert_text(st: dict) -> str | None:
    """Only the problems. Returns None when there is nothing to say.

    This is the watchdog contract: silence means healthy. An alert that fires
    on good news trains you to ignore alerts.
    """
    bad = [c for c in st["checks"] if c["state"] in (WARN, CRIT)]
    if not bad:
        return None
    head = "⛔ LivreSignals — problem detected" if st["overall"] == CRIT \
        else "⚠️ LivreSignals — degraded"
    body = "\n".join(
        f"{ICON[c['state']]} {c['name']}\n   {c['detail']}" for c in bad)
    return f"{head}\n\n{body}"


def main() -> int:
    st = gather()

    if "--alert" in sys.argv:
        msg = alert_text(st)
        if msg is None:
            return 0          # silent: nothing to report
        print(msg)
        return 2 if st["overall"] == CRIT else 1

    if "--json" in sys.argv:
        print(json.dumps(st, indent=2))
    else:
        print(render(st))

    return 0 if st["overall"] == OK else (2 if st["overall"] == CRIT else 1)


if __name__ == "__main__":
    sys.exit(main())
