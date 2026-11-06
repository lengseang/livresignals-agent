"""Audit cron wrappers for a failure mode that reports success (T0.12).

WHY THIS EXISTS
---------------
A quiet-on-success cron job is also quiet on failure unless something makes the
failure loud. Three mechanisms make it loud: a non-zero exit code, a printed
line, or neither being needed because the job is only a monitor. A wrapper of

    OUT="$(script 2>&1)"
    if [ -n "$OUT" ]; then echo "$OUT"; fi
    exit 0                        # <-- the rc was never captured

has removed the exit code; if the wrapped script also redirects its own stdout
it removed the line too. The scheduler then records `completed` and delivers
nothing: a crash is indistinguishable from a healthy no-op. That is the
2026-10-19 lesson-loop incident, which recurred verbatim in two live wrappers.

WHAT COUNTS
-----------
The rule is about the *status of a job that does work*. It fires only when all
three hold:

1. the wrapper captures the output of a command that runs a program
   (not `$(date ...)` / `$(dirname ...)`-style value lookups);
2. it never reads `$?` after that capture; and
3. it ends with a top-level `exit 0` — i.e. an `exit 0` that is not inside an
   `if`/`case` block, where a conditional `exit 0` is a legitimate "this
   failure is fine" decision (v2_gate_roll.sh's transient-read path).

An `exit 0` guarded by `if [ $RC -ne 0 ] ... exit $RC` is therefore fine, and
so is a script with no capture at all (bash returns the last command's status).

Nothing here executes a production script: it reads text and runs `bash -n`.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

# Commands whose result is a *value*, not the job's outcome. Capturing these
# says nothing about whether the job failed.
_VALUE_COMMANDS = re.compile(
    r"^\s*(?:date|dirname|basename|pwd|wc|grep|sed|awk|cat|head|tail|tr|cut|"
    r"nproc|whoami|hostname|git\s+rev-parse|uname)\b"
)

_ASSIGN_CAPTURE = re.compile(
    r"^(?P<indent>\s*)(?:export\s+)?(?P<var>[A-Za-z_][A-Za-z0-9_]*)="
    r"(?P<q>[\"']?)\$\("
)


def _command_substitution(line: str) -> str | None:
    """Return the text inside the first top-level `$(...)`, or None.

    Scans for the matching close paren, counting nested `$(`, so
    `OUT="$(bash "$X" 2>&1)"` is read correctly. A naive `[^)]*` regex stops at
    the first `)` and silently fails to see the capture at all — which would
    make this audit blind to the most ordinary quoting style.
    """
    start = line.find("$(")
    if start == -1:
        return None
    i, depth = start + 2, 1
    while i < len(line):
        ch = line[i]
        if ch == "$" and i + 1 < len(line) and line[i + 1] == "(":
            depth += 1
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return line[start + 2 : i]
        i += 1
    return None  # unterminated; bash -n will report it
_STATUS_READ = re.compile(r"^\s*(?:RC|rc)=\"?\$\?\"?\s*$")
_TOP_EXIT0 = re.compile(r"^exit\s+0\s*$")
_IF_OPEN = re.compile(r"^\s*(if|elif)\b.*\bthen\s*$|^\s*(if|elif)\b")
_CASE_OPEN = re.compile(r"^\s*case\b")
_BLOCK_CLOSE = re.compile(r"^\s*(fi|esac)\b")
# `fi` / `esac` anywhere in a line, so one-line `if ...; fi` closes correctly.
_CLOSE_TOKEN = re.compile(r"\b(fi|esac)\b")


def _top_level_exit0(lines: list[str]) -> list[int]:
    """Line numbers (1-based) of `exit 0` outside any if/case block.

    Handles the one-line form `if ...; then ...; fi`, which is common in these
    wrappers: the `fi` on the same line closes the `if` that line opened, so
    neither should change the depth. Counting them naively leaves depth at 1
    forever and the audit silently finds nothing.
    """
    depth = 0
    found: list[int] = []
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        opens_if = bool(_IF_OPEN.match(line))
        opens_case = bool(_CASE_OPEN.match(line))
        closes = len(_CLOSE_TOKEN.findall(stripped))

        if opens_if or opens_case:
            depth += 1
        # `elif ...; then` and `else` do not open a new block.
        depth = max(0, depth - closes)

        if _TOP_EXIT0.match(stripped) and depth == 0:
            found.append(i)
    return found


def _is_work_capture(cmd: str) -> bool:
    cmd = cmd.strip()
    if not cmd or _VALUE_COMMANDS.match(cmd):
        return False
    return True


def audit_text(text: str) -> list[str]:
    """Return a list of problems; empty means the wrapper propagates failure."""
    lines = text.splitlines()
    problems: list[str] = []

    captures: list[tuple[int, str, str]] = []  # (line_no, var, cmd)
    for i, line in enumerate(lines, start=1):
        m = _ASSIGN_CAPTURE.match(line)
        if not m:
            continue
        cmd = _command_substitution(line)
        if cmd is not None and _is_work_capture(cmd):
            captures.append((i, m.group("var"), cmd.strip()))

    if not captures:
        return problems

    status_lines = [i for i, ln in enumerate(lines, start=1) if _STATUS_READ.match(ln)]
    exit0_lines = _top_level_exit0(lines)

    # Only the LAST work capture matters: it is the one whose rc is still live
    # when the script decides how to exit.
    last_capture = captures[-1][0]
    read_after = [n for n in status_lines if n > last_capture]

    if not read_after and exit0_lines:
        first_work = captures[0]
        problems.append(
            f"captures `{first_work[2]}` (line {first_work[0]}) but never reads $?, "
            f"and exits 0 unconditionally at line {exit0_lines[-1]} — a crash "
            f"records as `completed` and delivers nothing"
        )
    return problems


def syntax_check(path: Path) -> str | None:
    """`bash -n`; returns an error string or None (skipped when bash is absent)."""
    bash = shutil.which("bash")
    if bash is None:
        return None
    proc = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        return (proc.stderr or "bash -n failed").strip().splitlines()[0]
    return None


def audit_dir(root: Path, skip: frozenset[str] = frozenset()) -> dict[str, list[str]]:
    """Audit every *.sh in `root`; returns {filename: [problems]} for failures only."""
    out: dict[str, list[str]] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.sh")):
        if path.name in skip:
            continue
        defects = audit_text(path.read_text(encoding="utf-8", errors="replace"))
        err = syntax_check(path)
        if err:
            defects.append(f"syntax: {err}")
        if defects:
            out[path.name] = defects
    return out


# Default location on the VPS. Absent in CI, which is why the test skips rather
# than fails when the directory does not exist.
DEFAULT_WRAPPER_DIR = Path("/home/ubuntu/hermes-home/scripts")

# Wrappers whose `exit 0` is intentional and documented. `hunt` reports only
# survivors and treats "nothing found" as silence; its rc is 0 by construction
# because it inspects $OUT for a survivors count rather than propagating a code.
INTENTIONALLY_SILENT = frozenset({"run_hunt.sh"})
