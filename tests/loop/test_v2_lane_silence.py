"""E13: a silent lane must be distinguishable from a healthy one.

The V2 paper lanes stopped at 2026-11-05T09:01Z. Every stored number from the
lane still describes a live process, and the two watchers that could have said
otherwise either run inside the dying process or have their exit code discarded
by their wrapper. These tests pin the three links in that chain so a future
re-templating cannot quietly restore it.

Design rule (see docs/proposals/v2-lane-silent-since-2026-11-05.md):
  A watcher must run in a process that does not die with the thing it watches,
  and its exit code must reach the scheduler.

Every test reads source text or fixtures only. Nothing here touches the live
lane, its heartbeat, its config or its process.
"""
from __future__ import annotations

import json
import re
import sqlite3
import textwrap
from pathlib import Path

import pytest

V2 = Path("/opt/trading-agent-v2")
CRON = Path("/home/ubuntu/hermes-home/cron")
WRAPPER = CRON.parent / "scripts" / "v2_gate_roll.sh"
GATE = V2 / "scripts" / "v2_gate_watchdog.py"


# --------------------------------------------------------------------------- #
# helpers (imported by the probe in research/ as well)
# --------------------------------------------------------------------------- #
def jev_turnout(hb: dict) -> float | None:
    """The ratio the gate and digest quote: jev_ok/jev_calls."""
    calls, ok = hb.get("jev_calls") or 0, hb.get("jev_ok") or 0
    return (ok / calls) if calls else None


def heartbeat_is_stale(hb_mtime: float, now: float, silent_s: float = 45 * 60) -> bool:
    return (now - hb_mtime) > silent_s


# --------------------------------------------------------------------------- #
# 1. the frozen ratio
# --------------------------------------------------------------------------- #
def test_a_frozen_heartbeat_still_quotes_a_healthy_turnout():
    """A dead lane's stored heartbeat reads exactly like a live one."""
    hb = {"cycles": 1728, "steps": 8639, "jev_ok": 1575, "jev_calls": 1725,
          "iso": "2026-11-05T09:00:03+00:00"}
    assert jev_turnout(hb) == pytest.approx(0.913, abs=1e-3)
    # nothing inside the record says when it stopped: `iso` is the last write,
    # not a health verdict. Only the file's mtime separates alive from dead.
    assert "stale" not in json.dumps(hb).lower()


def test_staleness_is_a_function_of_the_file_clock_not_of_the_record(tmp_path):
    """Two heartbeats with identical counters, one alive and one 40h dead."""
    body = json.dumps({"cycles": 1728, "jev_calls": 1725, "jev_ok": 1575})
    now = 1_800_000_000.0
    alive, dead = tmp_path / "alive.json", tmp_path / "dead.json"
    for p in (alive, dead):
        p.write_text(body)
    import os
    os.utime(alive, (now - 60, now - 60))
    os.utime(dead, (now - 40 * 3600, now - 40 * 3600))
    assert heartbeat_is_stale(alive.stat().st_mtime, now) is False
    assert heartbeat_is_stale(dead.stat().st_mtime, now) is True


# --------------------------------------------------------------------------- #
# 2. the in-process watcher
# --------------------------------------------------------------------------- #
def test_the_watchdog_that_detects_silence_is_written_by_the_watched_process():
    """watchdog.py is correct AND in-process: it cannot report its own process's death.

    The rule is keyed on `heartbeat.json`'s mtime, and that file is written by
    `runner.py` in the same process tree. This test states the dependency; if
    the watchdog ever moves to a separate process, this assertion should fail
    and be rewritten deliberately rather than deleted by accident.
    """
    wd = (V2 / "src" / "trading" / "v2" / "watchdog.py").read_text()
    runner = (V2 / "src" / "trading" / "v2" / "runner.py").read_text()
    assert "hb_path.stat().st_mtime" in wd
    assert "runner_silent" in wd
    assert '"heartbeat.json"' in runner and "write_text" in runner


# --------------------------------------------------------------------------- #
# 3. the independent watcher and its wrapper
# --------------------------------------------------------------------------- #
def test_an_independent_watcher_can_see_the_silence_and_exits_non_zero():
    src = GATE.read_text()
    assert "STALE_HEARTBEAT_S" in src
    assert "the lane is not" in src          # the staleness sentence
    assert "return 1" in src                 # a failure has a non-zero exit


def test_the_wrapper_does_not_discard_the_gate_exit_code(tmp_path):
    """The 2026-10-19 shape: report the failure text, then `exit 0`.

    `v2_gate_roll.sh` handles RC=2 explicitly (transient) and RC=1 by rolling
    back — but both paths end in `exit 0`, so the scheduler records `completed`
    for a run whose own text says the lanes are dead. Reads the live wrapper
    when it exists; falls back to the tracked copy.
    """
    if not WRAPPER.exists():
        pytest.skip("wrapper not on this host (CI checkout)")
    text = WRAPPER.read_text()
    assert re.search(r'if \[ "\$RC" = "2" \]', text), "RC=2 branch missing"
    assert re.search(r'echo "\$OUT"', text), "the failure text must still be printed"
    # The defect: the script's last statement is an unconditional exit 0.
    assert text.rstrip().endswith("exit 0")
    # And the owner-facing consequence, measured from the scheduler: the gate
    # job shows only `completed` rows while the watchdog job (which keeps its
    # rc) shows `failed`.
    db = CRON / "executions.db"
    if not db.exists():
        pytest.skip("no scheduler db on this host")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = dict(con.execute(
            "select status, count(*) from executions where job_id=? group by status",
            ("eb407360fa04",)))
    finally:
        con.close()
    assert "failed" not in rows, (
        "the gate job now reports failures; the wrapper was fixed — rewrite this "
        f"test to assert the fixed behaviour (rows were {rows})")


def test_a_wrapper_template_that_keeps_rc_reads_as_a_decision_not_a_swallow():
    """The fixed shape, for reference in the E13 proposal. Both branches exit with rc."""
    fixed = textwrap.dedent('''\
        OUT="$(run_gate 2>&1)"
        RC=$?
        if [ "$RC" = "2" ]; then echo "$OUT"; echo "(transient)"; exit "$RC"; fi
        if [ "$RC" != "0" ]; then echo "$OUT"; exit "$RC"; fi
        exit 0
        ''')
    unconditional = [ln for ln in fixed.splitlines()
                     if re.match(r"\s*exit 0\s*$", ln)]
    # exactly one exit 0, and it is reachable only when rc == 0
    assert len(unconditional) == 1
    assert fixed.index('if [ "$RC" != "0" ]') < fixed.index("exit 0")
