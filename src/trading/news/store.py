"""Append-only daily JSONL with cross-run dedupe.

Layout: <root>/<YYYY-MM-DD>.jsonl, dated by each record's fetched_utc (UTC).
Dedupe keys are rebuilt from the files at start-up, so the files are the only state.
"""
from __future__ import annotations

import json
from pathlib import Path


def dedupe_key(rec: dict) -> str | None:
    """news -> its canonical URL; fng -> its index time. None means never deduped."""
    if rec.get("kind") == "news" and rec.get("url"):
        return "url:" + rec["url"]
    if rec.get("kind") == "fng" and rec.get("index_time_utc"):
        return "fng:" + rec["index_time_utc"]
    return None


class NewsStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.seen: set[str] = set()
        for path in sorted(self.root.glob("????-??-??.jsonl")):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        key = dedupe_key(json.loads(line))
                    except ValueError:
                        continue                     # a torn last line from a crash; skip it
                    if key:
                        self.seen.add(key)

    def append(self, records: list[dict]) -> int:
        """Write records not seen before; return how many were written."""
        by_day: dict[str, list[str]] = {}
        for rec in records:
            key = dedupe_key(rec)
            if key is not None:
                if key in self.seen:
                    continue
                self.seen.add(key)
            by_day.setdefault(rec["fetched_utc"][:10], []).append(
                json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        self.root.mkdir(parents=True, exist_ok=True)
        for day, lines in by_day.items():
            with open(self.root / f"{day}.jsonl", "a", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")
        return sum(len(v) for v in by_day.values())
