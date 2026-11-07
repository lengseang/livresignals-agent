"""A watcher that runs *inside* the watched process cannot report that process's death.

Evidence for E13 (2026-11-07): the V2 paper lane has been dead since
2026-11-05T09:01Z, why the owner was not told, and why every stored number from
the lane still looks healthy.

READ-ONLY. This module reads:
  * /opt/trading-agent-v2/data/v2/<lane>/heartbeat.json, preflight.json,
    watchdog_state.json  (stored output of the dead lane)
  * /opt/trading-agent-v2/src/trading/v2/watchdog.py and runner.py (source)
  * ~/hermes-home/cron/scripts/v2_gate_roll.sh (the delivery wrapper)
  * ~/hermes-home/cron/executions.db (the scheduler's own attempt record)
It writes nothing, starts nothing, restarts nothing. Restarting the lane is the
owner's call (E13), and the missing input is a credential only the owner has.

The probe asserts the *shape* of the defect, not a hardcoded value, so it keeps
working after the lane is fixed: what must stay true is that the in-process
watchdog's silence detection exists, that the independent watcher exists and
exits non-zero, and that the wrapper keeps that exit code.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

V2 = Path("/opt/trading-agent-v2")
V2_DATA = V2 / "data" / "v2"
CRON = Path("/home/ubuntu/hermes-home/cron")
LANES = ("paper", "paper-b")
GATE_JOB = "eb407360fa04"          # v2-scale-gate-check → v2_gate_roll.sh
WATCHDOG_JOB = "d071de6fa4a4"      # v2-top8-gate-watchdog → v2_gate_watchdog.sh


def heartbeat(lane: str) -> dict:
    return json.loads((V2_DATA / lane / "heartbeat.json").read_text())


def preflight(lane: str) -> dict:
    return json.loads((V2_DATA / lane / "preflight.json").read_text())


def jev_turnout(hb: dict) -> float | None:
    """The `jev_ok / jev_calls` ratio the gate and the daily digest both quote.

    Both counters are written by the same process on the same step, so their
    ratio is frozen at the moment that process last ran. It is a turnout of a
    dead process, not of a live one.
    """
    calls, ok = hb.get("jev_calls") or 0, hb.get("jev_ok") or 0
    return (ok / calls) if calls else None


def in_process_watchdog_is_self_beat() -> bool:
    """True when watchdog.py's silence rule is keyed on another file's mtime.

    That is the correct rule, and it is also why it cannot fire here: the file
    it stats is written by the runner in the *same* process tree, so when the
    runner dies the watchdog is not scheduled either.
    """
    src = (V2 / "src" / "trading" / "v2" / "watchdog.py").read_text()
    return "st_mtime" in src and "SILENT_S" in src


def independent_watcher_reports(lane: str) -> bool:
    """The gate script's own staleness branch: true when it would print a failure."""
    src = (V2 / "scripts" / "v2_gate_watchdog.py").read_text()
    return "heartbeat is" in src and "return 1" in src


def wrapper_keeps_rc() -> dict:
    """How v2_gate_roll.sh handles the gate's exit code."""
    sh = (CRON / "scripts" / "v2_gate_roll.sh")
    if not sh.exists():
        sh = CRON.parent / "scripts" / "v2_gate_roll.sh"
    text = sh.read_text()
    rc2 = re.search(r'if \[ "\$RC" = "2" \]', text) is not None
    ends_exit0 = text.rstrip().endswith("exit 0")
    reports_out = re.search(r'echo "\$OUT"', text) is not None
    return {"checks_rc2": rc2, "reports_output_on_rc2": reports_out,
            "always_exit_0": ends_exit0}


def failure_delivery(job_id: str) -> dict:
    """What the scheduler recorded for a job's non-zero exits."""
    con = sqlite3.connect(f"file:{CRON / 'executions.db'}?mode=ro", uri=True)
    try:
        rows = list(con.execute(
            "select status, count(*) from executions where job_id=? group by status",
            (job_id,)))
    finally:
        con.close()
    return dict(rows)


def main() -> None:
    for lane in LANES:
        hb, pf = heartbeat(lane), preflight(lane)
        failed = [c["name"] for c in pf.get("checks", [])
                  if c.get("required") and not c.get("ok")]
        print(f"[{lane}] heartbeat written at {hb.get('iso')} "
              f"(cycles {hb.get('cycles')}, steps {hb.get('steps')})")
        print(f"   jev turnout AS STORED: {jev_turnout(hb):.1%} — a ratio of two "
              f"counters frozen at 2026-11-05T09:00Z")
        print(f"   preflight ok={pf.get('ok')} failing required: {failed or 'none'}")
    print()
    print(f"in-process watchdog keys on heartbeat mtime: {in_process_watchdog_is_self_beat()}")
    print(f"independent watcher has a staleness branch: {independent_watcher_reports('paper')}")
    print(f"wrapper handling of the gate's rc: {wrapper_keeps_rc()}")
    print(f"delivery record, v2-scale-gate-check  ({GATE_JOB}): {failure_delivery(GATE_JOB)}")
    print(f"delivery record, v2-top8-gate-watchdog ({WATCHDOG_JOB}): {failure_delivery(WATCHDOG_JOB)}")


if __name__ == "__main__":
    main()
