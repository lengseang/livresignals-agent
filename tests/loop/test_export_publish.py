"""The ledger export must keep publishing when the site repo moved on (front-end merges), and never force."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("export_ledger_json", ROOT / "scripts" / "export_ledger_json.py")
ex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ex)


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


def setup(tmp: Path):
    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = tmp / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "public" / "data").mkdir(parents=True)
    (seed / "public" / "data" / "ledger.json").write_text('{"records": 1}\n')
    (seed / "page.tsx").write_text("v1\n")
    git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "seed"); git(seed, "push", "-q", "origin", "main")
    site, other = tmp / "site", tmp / "other"                         # the VPS checkout; a front-end developer
    for d in (site, other):
        subprocess.run(["git", "clone", "-q", str(remote), str(d)], check=True, capture_output=True)
    return remote, site, other


def test_publish_succeeds_when_the_site_repo_moved_ahead(tmp_path):
    remote, site, other = setup(tmp_path)
    (other / "page.tsx").write_text("v2: terminal\n")                 # a front-end merge the VPS has not pulled
    git(other, "commit", "-qam", "front-end change"); git(other, "push", "-q", "origin", "main")
    (site / "public" / "data" / "ledger.json").write_text('{"records": 2}\n')
    assert ex.publish(site, 2, 1) == 0
    log = git(remote, "log", "--format=%s", "main")
    assert log.splitlines()[:2] == ["data: ledger export — 2 records, 1 closed", "front-end change"]


def test_a_real_conflict_fails_loudly_and_forces_nothing(tmp_path, capsys):
    remote, site, other = setup(tmp_path)
    (other / "public" / "data" / "ledger.json").write_text('{"records": 99}\n')
    git(other, "commit", "-qam", "someone else edited the ledger"); git(other, "push", "-q", "origin", "main")
    before = git(remote, "rev-parse", "main")
    (site / "public" / "data" / "ledger.json").write_text('{"records": 2}\n')
    assert ex.publish(site, 2, 1) == 3
    assert "pull --rebase failed" in capsys.readouterr().out
    assert git(remote, "rev-parse", "main") == before                   # remote untouched
    assert not (site / ".git" / "rebase-merge").exists() and not (site / ".git" / "rebase-apply").exists()
