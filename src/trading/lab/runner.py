"""Run every queued spec's jobs in parallel, gate them, and write a one-page digest.

    python -m trading.lab.runner --root data/local/lab --workers 6            # once
    python -m trading.lab.runner --root data/local/lab --workers 6 --every 30  # poll the queue

Each job is one replay (the verified engine, as a subprocess) plus one gate. All runs write to
the shared replay folder so every variant lands in the single global trial log, which keeps
the Deflated Sharpe trial count honest. Finished jobs are marked done and never re-run; a
failed job is recorded and retried on the next pass. Paper research only: nothing here
imports an exchange client.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from trading.lab import spec as lab_spec

SRC = Path(__file__).resolve().parents[2]
REPLAY_OUT = Path("data/local/replay")
HISTORY = Path("data/local/history/monthly")


def subprocess_replay(args: list[str], out_root: Path, history: Path = HISTORY) -> dict:
    t0 = time.monotonic()
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    p = subprocess.run([sys.executable, "-m", "trading.replay.run", *args, "--out", str(out_root),
                        "--history-root", str(history)], capture_output=True, text=True, env=env)
    run_dir = next((line.split("wrote ", 1)[1].strip() for line in p.stdout.splitlines() if line.startswith("wrote ")), None)
    return {"run_dir": run_dir, "rc": p.returncode, "seconds": round(time.monotonic() - t0, 1),
            "stderr_tail": p.stderr[-400:]}


def subprocess_gate(run_dir: str) -> dict:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    subprocess.run([sys.executable, "-m", "trading.replay.gate", "--run", run_dir],
                   capture_output=True, text=True, env=env)
    g = json.loads((Path(run_dir) / "gate.json").read_text(encoding="utf-8"))
    dsr = (g.get("checks") or {}).get("dsr") or {}
    return {"verdict": g.get("verdict"), "dsr": dsr.get("value"), "n_trials": dsr.get("n_trials"),
            "reasons": g.get("reasons", [])}


class Lab:
    def __init__(self, root: Path, workers: int = 6,
                 replay: Callable[[list[str], Path], dict] = subprocess_replay,
                 gate: Callable[[str], dict] = subprocess_gate, replay_out: Path = REPLAY_OUT):
        self.root = Path(root)
        self.workers = workers
        self.replay, self.gate, self.replay_out = replay, gate, Path(replay_out)
        for d in ("queue", "frozen", "done", "digests"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.results = self.root / "results.jsonl"
        self.ledger = self.root / "holdout-ledger.jsonl"

    def _pending(self) -> list[tuple[dict, dict]]:
        work = []
        for path in sorted((self.root / "queue").glob("*.json")):
            spec = lab_spec.freeze(path, self.root / "frozen")
            jobs = [j for j in lab_spec.expand(spec) if not (self.root / "done" / f"{j['job_id']}.json").exists()]
            if not jobs:
                continue
            if spec.get("open_holdout") and not (self.root / "frozen" / f"{spec['name']}.holdout").exists():
                lab_spec.holdout_permission(spec, self.ledger)
                (self.root / "frozen" / f"{spec['name']}.holdout").write_text("granted", encoding="utf-8")
            work += [(spec, j) for j in jobs]
        return work

    def _one(self, spec: dict, job: dict) -> dict:
        rep = self.replay(lab_spec.cli_args(spec, job), self.replay_out)
        row = {"at": datetime.now(timezone.utc).isoformat(), "spec": spec["name"], "strategy": spec["strategy"],
               "job_id": job["job_id"], "params": job["params"], "seconds": rep.get("seconds"),
               "run_dir": rep.get("run_dir"), "final_test": bool(spec.get("final_test"))}
        if rep.get("rc") != 0 or not rep.get("run_dir"):
            return {**row, "status": "error", "error": (rep.get("stderr_tail") or "")[-300:]}
        try:
            g = self.gate(rep["run_dir"])
        except Exception as e:  # noqa: BLE001 - a gate failure is recorded, and the job retried later
            return {**row, "status": "error", "error": f"gate: {type(e).__name__}: {e}"[:300]}
        return {**row, "status": "done", **g}

    def run_queue(self) -> dict:
        todo = self._pending()
        all_jobs = sum(len(lab_spec.expand(json.loads(p.read_text(encoding="utf-8"))))
                       for p in (self.root / "queue").glob("*.json"))
        rows: list[dict] = []
        with ThreadPoolExecutor(max(1, self.workers)) as pool:
            for row in pool.map(lambda sj: self._one(*sj), todo):
                rows.append(row)
                with self.results.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
                if row["status"] == "done":
                    (self.root / "done" / f"{row['job_id']}.json").write_text(json.dumps(row), encoding="utf-8")
        summary = {"jobs_run": sum(r["status"] == "done" for r in rows),
                   "jobs_failed": sum(r["status"] == "error" for r in rows),
                   "jobs_skipped": all_jobs - len(todo)}
        self._digest(rows, summary)
        return summary

    def _digest(self, rows: list[dict], summary: dict) -> Path:
        now = datetime.now(timezone.utc)
        path = self.root / "digests" / f"digest-{now:%Y-%m-%d}.md"
        lines = [f"## Lab pass {now:%Y-%m-%d %H:%M} UTC", "",
                 f"run {summary['jobs_run']} · failed {summary['jobs_failed']} · already done {summary['jobs_skipped']}",
                 "All specs pre-registered (frozen sha256 in frozen/). REPLAY research, not evidence of edge.", ""]
        if rows:
            lines += ["| spec | params | verdict | DSR | N | s |", "|---|---|---|---|---|---|"]
            for r in sorted(rows, key=lambda r: (r["status"] != "done", -(r.get("dsr") or -1))):
                dsr = r.get("dsr")
                lines.append(f"| {r['spec']} | {json.dumps(r['params'])} | {r.get('verdict') or r['status'].upper()} | "
                             f"{'—' if dsr is None else f'{dsr:.3f}'} | {r.get('n_trials') or '—'} | {r.get('seconds') or '—'} |")
            passes = [r for r in rows if r.get("verdict") == "PASS"]
            lines += ["", f"**PASS: {len(passes)}**" + (" — AI audit required before anything else." if passes else "")]
            for r in rows:
                if r["status"] == "error":
                    lines.append(f"- error {r['job_id']}: `{(r.get('error') or '').splitlines()[-1] if r.get('error') else ''}`")
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")
        return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="data/local/lab")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--every", type=float, default=0, help="minutes between queue polls; 0 = run once")
    ap.add_argument("--replay-out", default=str(REPLAY_OUT),
                    help="where runs and the trial log go; use a scratch dir for smoke tests so real N isn't inflated")
    a = ap.parse_args()
    lab = Lab(Path(a.root), workers=a.workers, replay_out=Path(a.replay_out))
    while True:
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {lab.run_queue()}", flush=True)
        if not a.every:
            break
        time.sleep(a.every * 60)


if __name__ == "__main__":
    main()
