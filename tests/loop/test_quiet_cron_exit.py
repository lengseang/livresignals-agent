"""T0.12 — a quiet cron job must still be able to report a failure.

The 2026-10-19 lesson-loop incident: the wrapper ended in `exit 0`, so a crash
on every run reported `last_status: ok` for 24 hours. The wrapper was fixed;
the other quiet jobs were never audited. These tests encode the rule, and the
live-directory test is the regression guard for the two wrappers that still
carry the old shape.

Nothing here runs a production script. The behavioural tests build throwaway
wrappers in tmp_path from the two templates and run bash on those only.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from trading.ops.cron_exit_audit import (  # noqa: E402
    DEFAULT_WRAPPER_DIR,
    INTENTIONALLY_SILENT,
    audit_dir,
    audit_text,
    syntax_check,
)

BASH = shutil.which("bash") or ""
needs_bash = pytest.mark.skipif(not BASH, reason="bash not available")

# The pre-2026-10-19 template, verbatim in shape.
LEGACY_TEMPLATE = """#!/usr/bin/env bash
OUT="$(bash "$INNER" 2>&1)"
if [ -n "$OUT" ]; then printf '[%s] %s\\n' SCAN "$OUT"; fi
exit 0
"""

# The lesson-loop template: keeps the rc, warns on failure, stays quiet on a no-op.
FIXED_TEMPLATE = """#!/usr/bin/env bash
OUT="$(bash "$INNER" 2>&1)"
RC=$?
if [ -n "$OUT" ]; then printf '[%s] %s\\n' LEARNING "$OUT"; fi
if [ "$RC" -ne 0 ]; then echo "[LEARNING] job exited $RC"; fi
exit "$RC"
"""

# A wrapper whose conditional exit 0 is a *decision* (v2_gate_roll.sh's
# transient-read path) — must not be flagged.
CONDITIONAL_TEMPLATE = """#!/usr/bin/env bash
OUT="$(bash "$INNER" 2>&1)"
RC=$?
if [ "$RC" = "2" ]; then
  echo "$OUT"
  exit 0
fi
if [ "$RC" = "0" ]; then
  exit 0
fi
echo "FAILED"
exit "$RC"
"""


def _crash(tmp: Path) -> Path:
    p = tmp / "crash.sh"
    p.write_text("#!/bin/bash\necho 'boom' >&2\nexit 1\n")
    return p


def _silent(tmp: Path) -> Path:
    p = tmp / "silent.sh"
    p.write_text("#!/bin/bash\nexit 0\n")
    return p


# ---------------------------------------------------------------- static rules


def test_legacy_template_is_flagged():
    """The exact shape that hid the 2026-10-19 crash is caught."""
    problems = audit_text(LEGACY_TEMPLATE)
    assert problems, "the exit-0-after-capture shape must be reported"
    assert "never reads $?" in problems[0]


def test_fixed_template_is_clean():
    assert audit_text(FIXED_TEMPLATE) == []


def test_conditional_exit0_is_not_flagged():
    """A guarded `exit 0` is a decision, not a swallowed failure."""
    assert audit_text(CONDITIONAL_TEMPLATE) == []


def test_value_captures_are_ignored():
    """`$(date ...)`/`$(dirname ...)` say nothing about the job's outcome."""
    text = (
        "#!/usr/bin/env bash\n"
        'SEED=$(date -u +%-j)\n'
        'DIR=$(dirname "$0")\n'
        'exec "$DIR/run.py" --seed "$SEED"\n'
    )
    assert audit_text(text) == []


def test_script_without_capture_is_not_flagged():
    """No capture -> bash returns the last command's status; nothing to audit."""
    text = "#!/bin/bash\ncd /opt/x || exit 1\nexec python3 run.py\n"
    assert audit_text(text) == []


def test_capture_then_rc_check_then_exit_zero_is_clean():
    """Reading $? and deciding to exit 0 *after* reporting is allowed."""
    text = (
        "#!/usr/bin/env bash\n"
        'OUT="$(run.py 2>&1)"\n'
        "RC=$?\n"
        'if [ "$RC" -ne 0 ]; then echo "$OUT"; fi\n'
        "exit 0\n"
    )
    assert audit_text(text) == []


# ----------------------------------------------------------- behavioural proof


@needs_bash
def test_legacy_template_reports_success_when_inner_crashes(tmp_path):
    """The bug, demonstrated: a crash exits 0 under the old template."""
    inner = _crash(tmp_path)
    w = tmp_path / "legacy.sh"
    w.write_text(LEGACY_TEMPLATE.replace("$INNER", str(inner)))
    proc = subprocess.run([BASH, str(w)], capture_output=True, text=True)
    assert proc.returncode == 0, "legacy template must lose the rc (this is the defect)"
    assert "boom" in proc.stdout, "and it prints the trace as if it were a normal line"


@needs_bash
def test_fixed_template_propagates_the_crash(tmp_path):
    inner = _crash(tmp_path)
    w = tmp_path / "fixed.sh"
    w.write_text(FIXED_TEMPLATE.replace("$INNER", str(inner)))
    proc = subprocess.run([BASH, str(w)], capture_output=True, text=True)
    assert proc.returncode == 1
    assert "exited 1" in proc.stdout


@needs_bash
def test_fixed_template_stays_silent_on_noop(tmp_path):
    """Quiet-on-success is the contract this must not break."""
    inner = _silent(tmp_path)
    w = tmp_path / "fixed.sh"
    w.write_text(FIXED_TEMPLATE.replace("$INNER", str(inner)))
    proc = subprocess.run([BASH, str(w)], capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", f"no-op must emit nothing, got {proc.stdout!r}"


# --------------------------------------------------------------- live wrappers


@pytest.mark.skipif(
    not DEFAULT_WRAPPER_DIR.is_dir(), reason="cron wrapper dir not present on this host"
)
def test_every_live_wrapper_parses():
    bad = {}
    for path in sorted(DEFAULT_WRAPPER_DIR.glob("*.sh")):
        err = syntax_check(path)
        if err:
            bad[path.name] = err
    assert not bad, f"wrappers with syntax errors: {bad}"


@pytest.mark.skipif(
    not DEFAULT_WRAPPER_DIR.is_dir(), reason="cron wrapper dir not present on this host"
)
def test_no_live_wrapper_discards_a_failure():
    """The regression guard. Lists the offenders rather than asserting a count,
    so a fix in one wrapper does not hide a new defect in another."""
    defects = audit_dir(DEFAULT_WRAPPER_DIR, skip=INTENTIONALLY_SILENT)
    assert not defects, (
        "these wrappers cannot report a failure to the owner:\n"
        + "\n".join(f"  {k}: {v}" for k, v in defects.items())
    )
