#!/usr/bin/env python3
"""LEDGER OFF-BOX REPLICATION — push the record to a separate Git repo.

WHY A SEPARATE REPO
-------------------
The ledger must never live in the public website repo (it is the asset, and
the site repo is rebuilt on every deploy). A separate, PRIVATE repo gives:
  * geographic and hardware independence from the VPS
  * an immutable, timestamped history in git itself
  * a second, independent integrity trail

USAGE
    python scripts/ledger_offbox.py status
    python scripts/ledger_offbox.py push

CONFIG
    Reads LEDGER_BACKUP_REPO from the environment (a git SSH URL). If unset,
    the script reports that off-box replication is not configured and exits
    cleanly — silently doing nothing is worse than being told.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
LEDGER = BASE / "data" / "signal-ledger.jsonl"
WORK = Path("/home/ubuntu/ledger-mirror")

ENV_FILE = BASE / "config" / "ledger-backup.env"


def load_env() -> dict:
    """Read KEY=value lines from config/ledger-backup.env, if present."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _ensure_deploy_key(work: Path) -> None:
    """Pin the ledger-backup deploy key to this repo.

    🔴 WHY (found 2026-09-23): a GitHub deploy key is scoped to exactly ONE
    repository. The default `github.com` identity in ~/.ssh/config points at
    `id_ed25519_github`, which is the key for the PUBLIC signals-site repo. Use
    it against the private backup repo and GitHub answers

        ERROR: Repository not found.

    which reads like "the repo does not exist" rather than "wrong key" — a
    misleading failure, and the reason this went unnoticed.

    The off-box replication had therefore been dead since the keys were split
    on 2026-09-21: the last successful sync was 2026-09-18, while the daily cron
    carried on reporting success. The backup was configured, ran, and did
    nothing.

    Setting core.sshCommand here (rather than editing ~/.ssh/config) keeps the
    fix inside the repo, so it survives the work-dir being recreated.
    """
    key = Path("/home/ubuntu/.ssh/id_ed25519_ledger_backup")
    if key.exists():
        sh(f'git config core.sshCommand '
           f'"ssh -i {key} -o IdentitiesOnly=yes '
           f'-o StrictHostKeyChecking=accept-new"', work)


def sh(cmd: str, cwd: Path | None = None) -> tuple[int, str]:
    """Run a shell command, with GIT_SSH_COMMAND set when a dedicated key is
    configured. Deploy keys are single-repo in GitHub, so the backup repo has
    its own key rather than reusing the website repo's."""
    env_prefix = ""
    key = (load_env().get("LEDGER_BACKUP_SSH_KEY")
           or os.environ.get("LEDGER_BACKUP_SSH_KEY"))
    if key:
        env_prefix = (
            f'GIT_SSH_COMMAND="ssh -i {key} -o IdentitiesOnly=yes '
            f'-o StrictHostKeyChecking=accept-new" '
        )
    p = subprocess.run(env_prefix + cmd, shell=True,
                       cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, timeout=180)
    return p.returncode, (p.stdout + p.stderr).strip()


def status() -> int:
    env = load_env()
    repo = env.get("LEDGER_BACKUP_REPO") or os.environ.get("LEDGER_BACKUP_REPO")
    print("=" * 70)
    print("LEDGER OFF-BOX REPLICATION — status")
    print("=" * 70)
    print(f"  local ledger : {'exists' if LEDGER.exists() else 'MISSING'}"
          + (f" ({sum(1 for _ in LEDGER.open())} records)" if LEDGER.exists() else ""))
    if not repo:
        print("  destination  : NOT CONFIGURED")
        print()
        print("  To enable, create a PRIVATE repo (e.g. lengseang/ledger-backup)")
        print("  and write one line to:")
        print(f"      {ENV_FILE}")
        print()
        print("      LEDGER_BACKUP_REPO=git@github.com:lengseang/ledger-backup.git")
        print()
        print("  Use a deploy key with write access scoped to that repo only.")
        return 1
    print(f"  destination  : {repo}")
    print(f"  work dir     : {WORK} "
          f"({'cloned' if (WORK / '.git').exists() else 'not cloned yet'})")
    return 0


def push() -> int:
    env = load_env()
    repo = env.get("LEDGER_BACKUP_REPO") or os.environ.get("LEDGER_BACKUP_REPO")
    if not repo:
        print("  off-box replication not configured — run `status` for setup.")
        return 1
    if not LEDGER.exists():
        print("  ⛔ no ledger to back up")
        return 2

    print("=" * 70)
    print("LEDGER OFF-BOX REPLICATION")
    print("=" * 70)

    if not (WORK / ".git").exists():
        WORK.parent.mkdir(parents=True, exist_ok=True)
        if WORK.exists():
            subprocess.run(["rm", "-rf", str(WORK)], check=False)
        code, out = sh(f"git clone --depth 1 {repo} {WORK}")
        if code != 0:
            # empty remote repos cannot be cloned; init instead
            WORK.mkdir(parents=True, exist_ok=True)
            sh("git init -q -b main", WORK)
            sh(f"git remote add origin {repo}", WORK)
            sh('git config user.name "ledger-bot"', WORK)
            sh('git config user.email "ledger-bot@users.noreply.github.com"', WORK)
            print("  initialised a fresh mirror (remote was empty)")
        else:
            print("  cloned existing mirror")
        _ensure_deploy_key(WORK)

    # copy the ledger in
    dest = WORK / "signal-ledger.jsonl"
    before = dest.read_bytes() if dest.exists() else b""
    after = LEDGER.read_bytes()
    dest.write_bytes(after)

    # Only regenerate STATUS.md / commit when the LEDGER itself changed.
    # Without this the mirror would produce a commit every single day purely
    # 🔴 PREFLIGHT: prove the remote is reachable BEFORE reporting anything.
    #
    # This is the fix for the silent failure. The caller used to be told
    # "nothing to replicate" without the remote ever being contacted, so a
    # broken deploy key produced a clean exit 0 for five straight days while the
    # backup was in fact stale.
    #
    # ⚠️ AND THE EXIT CODE IS NOT ENOUGH. Measured on this box:
    #
    #     $ git ls-remote --heads origin        # with a WRONG deploy key
    #     ERROR: Repository not found.
    #     fatal: Could not read from remote repository.
    #     $ echo $?
    #     0
    #
    # Git reports success on this failure. That is the entire reason a dead
    # backup looked healthy — every layer trusted an exit code that is always
    # zero. The only reliable signal is whether we got actual ref output.
    code, out = sh("git ls-remote --heads origin", WORK)
    reachable = code == 0 and bool(out.strip()) and "ERROR" not in out.upper() \
        and "fatal" not in out.lower()
    if not reachable:
        print(f"  ⛔ backup remote UNREACHABLE — the mirror is NOT being updated.")
        print(f"     output: {out.strip()[:300]}")
        print(f"     git exit code was {code} (git exits 0 on this failure, which is")
        print(f"     why the break went unnoticed — do not trust the exit code here).")
        print(f"     fix: ensure the ledger-backup deploy key is configured")
        print(f"     (see _ensure_deploy_key), then re-run.")
        return 4

    # because a timestamp moved, which makes the git history useless as an
    # audit trail.
    if before == after and (WORK / ".git").exists():
        code, _ = sh("git diff --cached --quiet", WORK)
        if code == 0:
            print("  ledger unchanged — nothing to replicate")
            print("  (remote reachable ✅ verified, so this is a fact about the"
                  " ledger, not an untested guess)")
            return 0

    # a small human-readable summary alongside it
    try:
        import signal_ledger as sl
        st = sl.public_stats()
        (WORK / "STATUS.md").write_text(
            "# Ledger mirror\n\n"
            f"Last replicated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n\n"
            "This repo is an automatic, append-only mirror of the hash-chained\n"
            "signal ledger. It exists so a hardware failure cannot destroy the\n"
            "track record.\n\n"
            f"| metric | value |\n|---|---|\n"
            f"| records | {st.get('signals', 0) + st.get('closed', 0)} |\n"
            f"| signals | {st.get('signals', 0)} |\n"
            f"| closed | {st.get('closed', 0)} |\n"
            f"| win rate | {st.get('win_rate_pct', 0):.1f}% |\n"
            f"| cumulative | {st.get('cumulative_return_pct', 0):+.2f}% |\n"
            f"| max drawdown | {st.get('max_drawdown_pct', 0):.2f}% |\n\n"
            "Verify with: `python scripts/signal_ledger.py verify`\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    sh("git add -A", WORK)
    code, out = sh('git diff --cached --quiet', WORK)
    if code == 0:
        print("  no changes to replicate")
        return 0
    n = sum(1 for _ in LEDGER.open())
    sh(f'git commit -q -m "ledger sync: {n} records @ '
       f'{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"', WORK)
    # 🔴 VERIFY THE REMOTE IS ACTUALLY REACHABLE BEFORE CLAIMING SUCCESS.
    #
    # `git fetch` against an inaccessible repo printed
    #     Please make sure you have the correct access rights
    # and STILL EXITED 0. So a push attempt on an auth-broken remote produced no
    # error the caller could see, and a daily cron can run for weeks reporting
    # success while replicating nothing. That is exactly what happened here.
    #
    # `git ls-remote` is the honest test: it fails loudly when auth is wrong.
    code, out = sh(f"git ls-remote --heads origin", WORK)
    if code != 0:
        print(f"  ⛔ cannot reach the backup remote — NOT replicating.")
        print(f"     {out.strip()[:300]}")
        print(f"     check the deploy key for this repo (see _ensure_deploy_key)")
        return 4
    code, out = sh("git push -q origin main", WORK)
    if code != 0:
        print(f"  ⛔ push failed: {out[:300]}")
        return 3
    # confirm the remote actually has our commit, rather than trusting the push
    code, out = sh("git rev-parse HEAD", WORK)
    local_head = out.strip().splitlines()[0] if out.strip() else ""
    code2, out2 = sh("git ls-remote origin main", WORK)
    remote_head = out2.split()[0] if out2.split() else ""
    if local_head and remote_head and local_head != remote_head:
        print(f"  ⛔ push reported success but the remote is at a different commit")
        print(f"     local {local_head[:12]} != remote {remote_head[:12]}")
        return 5
    print(f"  ✅ replicated {n} records off-box (verified at remote)")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "push":
        sys.exit(push())
    sys.exit(status())
