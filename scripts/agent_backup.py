#!/usr/bin/env python3
"""AGENT CODE BACKUP — push the trading agent repo off-box.

WHY THIS EXISTS
---------------
The signal ledger was already replicated off-box (scripts/ledger_offbox.py), but
the CODE and CONFIG were not: /opt/trading-agent had no git remote at all, so a
disk failure would have destroyed the source with nothing to restore from.

The ledger is the track record. The code is how the track record keeps being
produced. Losing either is fatal, so both now leave the box.

DESIGN NOTES
------------
* Uses a DEDICATED deploy key (id_ed25519_agent_backup) scoped to this ONE
  repository, not the general GitHub token. A GitHub deploy key cannot read any
  other repo, so if this key leaks the blast radius is one backup repo.
* Never trusts a git exit code. Measured behaviour on this box: `git fetch` and
  `git ls-remote` both print errors and STILL EXIT 0. That exact trap is what let
  the ledger replication sit dead for five days while every check said fine. So
  every result here is judged on OUTPUT, and the remote commit is compared
  against the local one after pushing.
* Fails loudly with a non-zero exit so a cron surfaces the problem instead of
  silently succeeding.

USAGE
    python scripts/agent_backup.py          # push if there are changes
    python scripts/agent_backup.py status   # report only, change nothing
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/opt/trading-agent")
KEY = Path("/home/ubuntu/.ssh/id_ed25519_agent_backup")
SSH = (f"ssh -i {KEY} -o IdentitiesOnly=yes "
       f"-o StrictHostKeyChecking=accept-new")
REMOTE = "git@github.com:lengseang/livresignals-agent.git"

# Files/dirs that must NEVER be committed. Checked before every push, because a
# single `git add -A` with a broken .gitignore would publish live API keys
# irreversibly — git history keeps them forever even after deletion.
FORBIDDEN = (".env", ".env.local", ".env.production")


def sh(cmd: str, timeout: int = 180) -> tuple[int, str]:
    p = subprocess.run(
        cmd, shell=True, cwd=str(REPO), capture_output=True, text=True,
        timeout=timeout, env={**os.environ, "GIT_SSH_COMMAND": SSH})
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _remote_reachable() -> tuple[bool, str]:
    """True only if we get real ref output. Exit code is not trustworthy."""
    code, out = sh(f"git ls-remote --heads {REMOTE}")
    reachable = (code == 0 and bool(out.strip())
                 and "ERROR" not in out.upper() and "fatal" not in out.lower())
    return reachable, out.strip()


def _leaked_secrets() -> list[str]:
    """Any staged file that looks like a credential file."""
    code, out = sh("git diff --cached --name-only")
    staged = [f for f in out.splitlines() if f.strip()]
    bad = []
    for f in staged:
        name = Path(f).name
        if name in FORBIDDEN or name.startswith(".env.") and not name.endswith(".example"):
            bad.append(f)
        if name.endswith(".env"):
            bad.append(f)
    return bad


def status() -> int:
    print("=" * 70)
    print("AGENT CODE BACKUP — status")
    print("=" * 70)
    ok, out = _remote_reachable()
    print(f"  remote      : {REMOTE}")
    print(f"  deploy key  : {KEY}  ({'present' if KEY.exists() else 'MISSING'})")
    print(f"  reachable   : {'✅ yes' if ok else '🔴 NO'}")
    if not ok:
        print(f"  output      : {out[:200]}")
    code, local = sh("git rev-parse HEAD")
    lh = local.strip().splitlines()[0] if local.strip() else ""
    _, rem = sh(f"git ls-remote {REMOTE} main")
    rh = rem.split()[0] if rem.split() else ""
    print(f"  local HEAD  : {lh[:12]}")
    print(f"  remote HEAD : {rh[:12]}")
    print(f"  in sync     : {'✅ yes' if lh and lh == rh else '🔴 no'}")
    code, d = sh("git status --porcelain")
    n = len([x for x in d.splitlines() if x.strip()])
    print(f"  uncommitted : {n} file(s)")
    return 0 if ok else 4


def push() -> int:
    ok, out = _remote_reachable()
    if not ok:
        print("  ⛔ remote UNREACHABLE — code is NOT being backed up.")
        print(f"     output: {out[:250]}")
        print(f"     git exits 0 on this failure, so the exit code proves nothing.")
        return 4

    code, d = sh("git status --porcelain")
    dirty = [x for x in d.splitlines() if x.strip()]
    if not dirty:
        print("  nothing to back up — working tree clean"
              " (remote reachable ✅ verified)")
        return 0

    # SAFETY GATE: refuse to commit credential files, ever.
    sh("git add -A")
    bad = _leaked_secrets()
    if bad:
        sh("git reset")
        print(f"  ⛔ REFUSING TO PUSH — credential file(s) staged: {bad}")
        print(f"     .gitignore should have blocked these; fix it before backing up.")
        return 6

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sh(f'git commit -q -m "auto-backup: {ts}"')
    code, out = sh("git push -q origin main")
    if code != 0:
        print(f"  ⛔ push failed: {out[:250]}")
        return 3

    # verify the remote actually moved, rather than trusting the push
    code, local = sh("git rev-parse HEAD")
    lh = local.strip().splitlines()[0] if local.strip() else ""
    _, rem = sh(f"git ls-remote {REMOTE} main")
    rh = rem.split()[0] if rem.split() else ""
    if not lh or lh != rh:
        print(f"  ⛔ push claimed success but remote differs")
        print(f"     local {lh[:12]} vs remote {rh[:12]}")
        return 5
    print(f"  ✅ backed up {len(dirty)} change(s) — verified at remote {rh[:12]}")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "push"
    sys.exit(status() if cmd == "status" else push())
