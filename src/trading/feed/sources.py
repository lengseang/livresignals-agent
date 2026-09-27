"""Read-only access to what the paper stack writes under data/local. Nothing here opens a file for writing.

- signals/<YYYY-MM-DD>.jsonl                 live scanner (LIVE_PAPER), tailed by byte offset, whole lines only
- signals/once-replay/<run_id>.jsonl         scanner --once-replay output (REPLAY)
- replay/<run_id>/{summary,gate}.json, events.jsonl    replay runs (REPLAY)
- ticks/<YYYY-MM-DD>/{book,trade,gap,clock}/<HH>.jsonl[.gz]   recorder (quotes, data quality, clock offset)
"""
from __future__ import annotations

import gzip
import json
import re
import time
from pathlib import Path

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
LIVE_ID_RE = re.compile(r"^live-(\d{4}-\d{2}-\d{2})$")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _jsonl(path: Path):
    op = gzip.open if path.suffix == ".gz" else open
    try:
        with op(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue            # torn line: skipped, never guessed
    except (OSError, EOFError):
        return


class DataRoot:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.replay = self.root / "replay"
        self.signals = self.root / "signals"
        self.ticks = self.root / "ticks"

    # ---- REPLAY ----------------------------------------------------------------------------------------
    def replay_runs(self) -> dict[str, dict]:
        runs: dict[str, dict] = {}
        if self.replay.is_dir():
            for d in sorted(self.replay.iterdir()):
                if d.is_dir() and RUN_ID_RE.match(d.name) and (d / "summary.json").exists():
                    runs[d.name] = {"kind": "replay_run", "dir": d}
        once = self.signals / "once-replay"
        if once.is_dir():
            for f in sorted(once.glob("*.jsonl")):
                if RUN_ID_RE.match(f.stem) and f.stem not in runs:
                    runs[f.stem] = {"kind": "scanner_once_replay", "file": f}
        return runs

    def replay_run(self, run_id: str) -> dict | None:
        if not RUN_ID_RE.match(run_id) or run_id.startswith("live-"):
            return None
        return self.replay_runs().get(run_id)       # looked up by listing, never by joining untrusted input

    def replay_events(self, run: dict) -> list[dict]:
        evs = list(_jsonl(run["dir"] / "events.jsonl"))
        order = {"strategy_signal": 0, "signal": 1, "close": 2}
        # events.jsonl is grouped by source (signals, then each account); the feed replays it in market time
        return sorted(evs, key=lambda e: (e.get("ts_ms") or 0, order.get(e.get("type"), 3)))

    # ---- LIVE_PAPER -------------------------------------------------------------------------------------
    def live_days(self) -> list[str]:
        days = set()
        if self.signals.is_dir():
            days |= {f.stem for f in self.signals.glob("*.jsonl") if _DAY_RE.match(f.stem)}
        if self.ticks.is_dir():
            days |= {d.name for d in self.ticks.iterdir() if d.is_dir() and _DAY_RE.match(d.name)}
        return sorted(days)

    def signals_file(self, day: str) -> Path:
        return self.signals / f"{day}.jsonl"

    def _hour_files(self, day: str, kind: str) -> dict[str, Path]:
        d = self.ticks / day / kind
        out: dict[str, Path] = {}
        if d.is_dir():
            for f in sorted(d.iterdir()):
                hour = f.name.split(".", 1)[0]
                if f.name.endswith(".jsonl.gz"):
                    out.setdefault(hour, f)
                elif f.name.endswith(".jsonl"):
                    out[hour] = f            # the open hour wins until the recorder gzips it
        return out

    def latest_by_symbol(self, day: str, kind: str, tail_bytes: int = 262_144) -> dict[str, dict]:
        """Latest record per symbol from the tail of the newest uncompressed hour file (the one being written)."""
        files = self._hour_files(day, kind)
        if not files:
            return {}
        f = files[max(files)]
        if f.suffix == ".gz":
            return {}                        # recorder not writing this day now: no live quotes
        try:
            with open(f, "rb") as fh:
                size = fh.seek(0, 2)
                fh.seek(max(0, size - tail_bytes))
                raw = fh.read()
        except OSError:
            return {}
        lines = raw.split(b"\n")
        if size > tail_bytes:
            lines = lines[1:]                # first line is partial
        out: dict[str, dict] = {}
        for line in lines:
            line = line.strip()              # tolerate CRLF
            if not line.endswith(b"}"):
                continue                     # unterminated last line
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "symbol" in r:
                out[r["symbol"]] = r
        return out

    def last_record(self, day: str, kind: str) -> dict | None:
        files = self._hour_files(day, kind)
        if not files:
            return None
        last = None
        for r in _jsonl(files[max(files)]):
            last = r
        return last

    def newest_mtime(self, day: str, kind: str) -> float | None:
        files = self._hour_files(day, kind)
        try:
            return max(f.stat().st_mtime for f in files.values()) if files else None
        except OSError:
            return None


class LineTail:
    """New complete lines of one append-only file since the last call (offset in bytes)."""

    def __init__(self, path: Path):
        self.path, self.offset = path, 0

    def poll(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            self.offset = 0                  # replaced file: start over (seq resets are the sink's business)
        if size == self.offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read(size - self.offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            return []                        # only a partial line so far
        self.offset += end + 1
        out = []
        for line in chunk[:end].split(b"\n"):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out


class ShadowTail:
    """P4a: the shadow scanner's chain files of one day, <root>/<lane>/<strategy>/<day>.jsonl, each tailed by byte
    offset. New books are discovered on every poll. Yields (lane, strategy, record)."""

    def __init__(self, root: Path | str | None, day: str):
        self.root, self.day = (Path(root) if root else None), day
        self.tails: dict[Path, LineTail] = {}

    def poll(self) -> list[tuple[str, str, dict]]:
        if self.root is None or not self.root.is_dir():
            return []
        out = []
        for f in sorted(self.root.glob(f"*/*/{self.day}.jsonl")):
            t = self.tails.setdefault(f, LineTail(f))
            out += [(f.parent.parent.name, f.parent.name, r) for r in t.poll()]
        return sorted(out, key=lambda x: (x[2].get("ts_event_ms") or 0, x[2].get("seq") or 0))


class GapTail:
    """Recorder gap records of one day, each yielded once, across the .jsonl -> .jsonl.gz rename at hour end."""

    def __init__(self, data: DataRoot, day: str):
        self.data, self.day = data, day
        self.seen: dict[str, int] = {}       # hour -> records already emitted

    def poll(self) -> list[dict]:
        out = []
        for hour, f in sorted(self.data._hour_files(self.day, "gap").items()):
            recs = list(_jsonl(f))
            n = self.seen.get(hour, 0)
            if len(recs) > n:
                out.extend(recs[n:])
                self.seen[hour] = len(recs)
        return out


def component_status(mtime: float | None, now: float | None = None) -> str:
    """ok if the file moved in the last 10 s, degraded within 120 s, else down (no file: down)."""
    if mtime is None:
        return "down"
    age = (now if now is not None else time.time()) - mtime
    return "ok" if age <= 10 else "degraded" if age <= 120 else "down"
