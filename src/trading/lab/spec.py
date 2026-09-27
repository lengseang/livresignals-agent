"""Experiment specs: validation, grid expansion, freezing, and the single-use holdout.

A spec is pre-registered: once frozen, any edit is refused, so a grid can't be quietly
changed after its results are seen. The holdout (the untouched final months) opens only for
a spec marked `final_test` with exactly one candidate, and each candidate may use it once.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime, timezone
from pathlib import Path

ALLOWED = {"name", "strategy", "grid", "fixed", "final_test", "open_holdout", "hypothesis", "notes", "priority"}


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def _sha(x) -> str:
    return hashlib.sha256(_canon(x).encode()).hexdigest()


def validate(spec: dict) -> dict:
    unknown = set(spec) - ALLOWED
    if unknown:
        raise ValueError(f"unknown spec keys: {sorted(unknown)}")
    for key in ("name", "strategy"):
        if not isinstance(spec.get(key), str) or not spec[key]:
            raise ValueError(f"spec needs a non-empty '{key}'")
    if not str(spec.get("hypothesis", "")).strip():
        raise ValueError("spec needs a 'hypothesis' written before any result is seen")
    grid = spec.get("grid", {})
    if not isinstance(grid, dict) or not all(isinstance(v, list) and v for v in grid.values()):
        raise ValueError("'grid' must map each parameter to a non-empty list")
    if spec.get("open_holdout") and len(expand(spec)) != 1:
        raise ValueError("a final_test with the holdout must be a single frozen candidate (grid of one)")
    return spec


def expand(spec: dict) -> list[dict]:
    grid = spec.get("grid", {})
    keys = list(grid)
    jobs = []
    for combo in itertools.product(*(grid[k] for k in keys)) if keys else [()]:
        params = dict(zip(keys, combo))
        ident = {"strategy": spec["strategy"], "params": params, "fixed": spec.get("fixed", {}),
                 "holdout": bool(spec.get("open_holdout"))}
        jobs.append({"job_id": f"{spec['name']}-{_sha(ident)[:10]}", "params": params})
    return jobs


def cli_args(spec: dict, job: dict) -> list[str]:
    """replay CLI flags: every key becomes --key-with-dashes value."""
    args = ["--strategy", spec["strategy"]]
    for k, v in {**spec.get("fixed", {}), **job["params"]}.items():
        args += [f"--{k.replace('_', '-')}", str(v)]
    if spec.get("open_holdout") and spec.get("final_test"):
        args.append("--open-holdout")
    return args


def freeze(path: Path, frozen_dir: Path) -> dict:
    """Freeze a queued spec the first time it is seen; refuse it if edited afterwards."""
    spec = validate(json.loads(Path(path).read_text(encoding="utf-8")))
    frozen_dir.mkdir(parents=True, exist_ok=True)
    target = frozen_dir / f"{spec['name']}.json"
    sha = _sha(spec)
    if target.exists():
        if json.loads(target.read_text(encoding="utf-8"))["sha256"] != sha:
            raise ValueError(f"spec '{spec['name']}' changed after it was frozen; queue it under a new name")
    else:
        target.write_text(json.dumps({"sha256": sha, "frozen_at": datetime.now(timezone.utc).isoformat(),
                                      "spec": spec}, indent=2), encoding="utf-8")
    return spec


def holdout_permission(spec: dict, ledger: Path) -> bool:
    """True if this spec may open the holdout; records the use. Raises when it may not."""
    if not spec.get("open_holdout"):
        return False
    if not spec.get("final_test"):
        raise ValueError("opening the holdout requires final_test: true")
    job = expand(spec)[0]
    key = _sha({"strategy": spec["strategy"], "params": job["params"], "fixed": spec.get("fixed", {})})
    used = set()
    if ledger.exists():
        used = {json.loads(line)["candidate"] for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()}
    if key in used:
        raise ValueError(f"holdout already used by this candidate ({spec['strategy']} {job['params']})")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"candidate": key, "spec": spec["name"], "strategy": spec["strategy"],
                            "params": job["params"], "at": datetime.now(timezone.utc).isoformat()}) + "\n")
    return True
