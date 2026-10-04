"""V2-16 deploy files: syntactically valid, isolated from the live box, and a failed preflight blocks the start."""
import os
import pathlib
import shutil
import subprocess

import pytest

D = pathlib.Path("deploy/v2")
ROOT = pathlib.Path.cwd()


@pytest.mark.parametrize("script", ["run-v2.sh", "ops-v2.sh"])
def test_scripts_parse(script):
    assert subprocess.run(["bash", "-n", str(D / script)]).returncode == 0


def test_ecosystem_is_v2_only_and_isolated():
    js = (D / "ecosystem.v2.config.js").read_text()
    names = [l.split('"')[1] for l in js.splitlines() if l.strip().startswith("name:")]
    assert names == ["v2-paper", "v2-watchdog", "v2-daily"]
    code = "\n".join(l for l in js.splitlines() if not l.strip().startswith("//"))
    assert "/opt/trading-agent/" not in code and "/opt/trading-agent-v2/" in code
    assert "LIVE_ENABLED" not in code and "max_memory_restart" in code
    if shutil.which("node"):
        assert subprocess.run(["node", "-e", f"require('{(D / 'ecosystem.v2.config.js').resolve()}')"]).returncode == 0


def test_env_example_has_no_secret_and_is_not_ignored():
    text = (D / "v2.env.example").read_text()
    assert "TYPESAFE_API_KEY=\n" in text and "apikey_" not in text
    r = subprocess.run(["git", "check-ignore", "-q", str(D / "v2.env.example")])
    assert r.returncode == 1                                  # 1 = NOT ignored: the example is versioned


def test_real_env_file_would_be_ignored():
    assert subprocess.run(["git", "check-ignore", "-q", "config/v2.env"]).returncode == 0


def test_a_failed_preflight_blocks_the_runner(tmp_path):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    for name, target in (("src", ROOT / "src"), ("deploy", ROOT / "deploy"), ("venv", ROOT / ".venv")):
        (root / name).symlink_to(target)
    (root / "config" / "v2.env").write_text(f"TYPESAFE_API_KEY=\nV2_OUT={root}/out\n")       # no key -> FAIL
    env = {**os.environ, "V2_ROOT": str(root), "V2_FAIL_SLEEP": "0"}
    env.pop("TYPESAFE_API_KEY", None)
    r = subprocess.run(["bash", str(D / "run-v2.sh")], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1 and "PREFLIGHT FAILED" in r.stdout and "jev_key_present" in r.stdout
    assert not (root / "out" / "heartbeat.json").exists()              # the runner never started
