"""Append-only hourly JSONL per stream kind, gzipped once the hour closes.

Layout: <root>/<YYYY-MM-DD>/<kind>/<HH>.jsonl(.gz), all in UTC.
"""
from __future__ import annotations

import gzip
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def _gzip_and_remove(path: Path) -> None:
    with open(path, "rb") as src, gzip.open(path.with_suffix(".jsonl.gz"), "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


class Recorder:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._open: dict[str, tuple[Path, object]] = {}   # kind -> (path, file)

    def _path(self, kind: str, ts_ns: int) -> Path:
        dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
        return self.root / dt.strftime("%Y-%m-%d") / kind / f"{dt:%H}.jsonl"

    def write(self, kind: str, record: dict, ts_ns: int) -> None:
        path = self._path(kind, ts_ns)
        current = self._open.get(kind)
        if current is None or current[0] != path:
            if current is not None:
                current[1].close()
                _gzip_and_remove(current[0])
            path.parent.mkdir(parents=True, exist_ok=True)
            current = (path, open(path, "a", encoding="utf-8", newline="\n"))
            self._open[kind] = current
        current[1].write(json.dumps(record, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        for _, f in self._open.values():
            f.flush()

    def close(self) -> None:
        for path, f in self._open.values():
            f.close()
            _gzip_and_remove(path)
        self._open.clear()
