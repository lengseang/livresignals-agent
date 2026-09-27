"""The shadow chain: append-only, hash-chained forward records (docs/loop/designs/P2-shadow-scanner.md §4).

<root>/<lane>/<strategy>/<YYYY-MM-DD>.jsonl. Each record gets `seq`, `prev_hash` and
`hash = sha256(canonical_json(record without hash) + prev_hash)`. The first record of a day links to the last hash of
the previous day; the first record ever links to the genesis hash of (strategy, forward_start). A record is written
the instant it happens (a signal before the next open fills it), so the chain commits to it before any outcome.
Files are only ever appended to; nothing here rewrites a line.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def canon(rec: dict) -> str:
    return json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def genesis(strategy: str, forward_start_ms: int) -> str:
    return hashlib.sha256(f"livr-shadow:{strategy}:{forward_start_ms}".encode()).hexdigest()


def link(rec: dict, prev_hash: str) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256((canon(body) + prev_hash).encode()).hexdigest()


def utc_day(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class ShadowChain:
    """One (lane, strategy) chain. Resumes from the last record on disk after a restart."""

    def __init__(self, root: Path | str, lane: str, strategy: str, forward_start_ms: int):
        self.dir = Path(root) / lane / strategy
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lane, self.strategy, self.forward_start_ms = lane, strategy, forward_start_ms
        self.seq, self.head = 0, genesis(strategy, forward_start_ms)
        self.resumed_from_ms: int | None = None        # set when an existing chain is continued after a restart
        days = sorted(self.dir.glob("*.jsonl"))
        if days:
            last = None
            with open(days[-1], encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last = line
            if last is not None:
                r = json.loads(last)
                self.seq, self.head = r["seq"], r["hash"]
                self.forward_start_ms = r.get("forward_start_ms", forward_start_ms)
                self.resumed_from_ms = r.get("ts_event_ms") or r.get("ts_emit_ms")

    def write(self, rec: dict) -> dict:
        """Chain and append `rec` (the Scanner's sink interface). `ts_event_ms` picks the day file."""
        t = rec.get("ts_event_ms") or rec.get("ts_emit_ms")
        self.seq += 1
        out = {**rec, "lane": self.lane, "chain_strategy": self.strategy, "forward_start_ms": self.forward_start_ms,
               "seq": self.seq, "prev_hash": self.head}
        out["hash"] = link(out, self.head)
        with open(self.dir / f"{utc_day(t)}.jsonl", "a", encoding="utf-8") as f:
            f.write(canon(out) + "\n")
            f.flush()
        self.head = out["hash"]
        return out


def verify(chain_dir: Path | str) -> dict:
    """Re-derive every hash. {"ok", "records", "head", "error"}; any altered byte breaks it."""
    chain_dir = Path(chain_dir)
    prev, n, start = None, 0, None
    for day in sorted(chain_dir.glob("*.jsonl")):
        with open(day, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    return {"ok": False, "records": n, "head": prev, "error": f"{day.name}:{i} is not JSON"}
                if prev is None:
                    start = r.get("forward_start_ms")
                    prev = genesis(r.get("chain_strategy", ""), start)
                if r.get("prev_hash") != prev:
                    return {"ok": False, "records": n, "head": prev, "error": f"{day.name}:{i} does not link"}
                if r.get("hash") != link(r, prev) or r.get("seq") != n + 1:
                    return {"ok": False, "records": n, "head": prev, "error": f"{day.name}:{i} was altered"}
                prev, n = r["hash"], n + 1
    return {"ok": True, "records": n, "head": prev, "error": None}
