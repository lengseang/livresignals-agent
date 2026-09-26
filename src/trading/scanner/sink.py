"""Append-only signal files. Records are T6.1 feed envelopes (docs/loop/designs/T6.1.md §4.1).

- DailySink:  LIVE_PAPER, <root>/<YYYY-MM-DD>.jsonl by the signal's market time (UTC), run_id
  "live-<date>"; `seq` continues across restarts on the same day (read from the file's last line).
- FileSink:   one offline run (mode REPLAY), one file, its own run_id.

Files are opened in append mode only; nothing here truncates or rewrites a line.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def utc_date(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _last_seq(path: Path) -> int:
    if not path.exists():
        return 0
    last = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    last = int(json.loads(line)["seq"])
                except (ValueError, KeyError, TypeError):
                    continue            # a torn last line is left as is; seq continues from the last good one
    return last


class _Base:
    def __init__(self) -> None:
        self._f = None
        self._path: Path | None = None
        self._seq = 0

    def _open(self, path: Path) -> None:
        if self._path == path:
            return
        self.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = _last_seq(path)
        self._f = open(path, "a", encoding="utf-8", newline="\n")
        self._path = path

    def _append(self, rec: dict, run_id: str) -> dict:
        self._seq += 1
        rec = {**rec, "seq": self._seq, "run_id": run_id}
        self._f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._f.flush()
        return rec

    @property
    def path(self) -> Path | None:
        return self._path

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None
            self._path = None


class DailySink(_Base):
    mode = "LIVE_PAPER"

    def __init__(self, root: Path | str) -> None:
        super().__init__()
        self.root = Path(root)

    def write(self, rec: dict) -> dict:
        day = utc_date(rec["ts_event_ms"])
        self._open(self.root / f"{day}.jsonl")
        return self._append(rec, f"live-{day}")


class FileSink(_Base):
    mode = "REPLAY"

    def __init__(self, path: Path | str, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id
        self._open(Path(path))          # created up front: an offline run leaves its file even with 0 signals

    def write(self, rec: dict) -> dict:
        return self._append(rec, self.run_id)
