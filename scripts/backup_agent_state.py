#!/usr/bin/env python3
"""Back up the agent's memory (state.db) without needing 420 MB of free space.

WHY THIS SHAPE
state.db is 420 MB but only 58 MB is message text. 86% is FTS index bloat:
  messages_fts_trigram_data    151 MB   (substring search)
  messages_fts_content          71 MB   (a SECOND copy of the text)
  messages_fts_trigram_content  71 MB   (a THIRD copy)
A full VACUUM INTO needs 410 MB free and this box has ~350 MB, so it failed and
filled the disk. But the content is what cannot be recreated — the indexes can
be rebuilt from it with `INSERT INTO messages_fts(messages_fts) VALUES('rebuild')`.

So: dump content + sessions to compressed JSONL, skip the indexes entirely.
Expected size: ~58 MB raw, far less compressed.

Read-only: opens the database with mode=ro and never writes to it.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path("/home/ubuntu/hermes-home/state.db")
OUTDIR = Path("/opt/trading-agent/backups/hermes-state")
KEEP = ("messages", "sessions", "session_model_usage", "delivery_obligations",
        "gateway_routing", "async_delegations")


def main() -> int:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    manifest = {"as_of_utc": datetime.now(timezone.utc).isoformat(),
                "source": str(DB), "source_bytes": DB.stat().st_size,
                "tables": {}, "note": "Content-only backup. FTS indexes are NOT "
                "included — rebuild with: INSERT INTO messages_fts(messages_fts) "
                "VALUES('rebuild');"}

    total_out = 0
    for table in KEEP:
        try:
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if not exists:
                continue
            rows = con.execute(f'SELECT * FROM "{table}"').fetchall()
            path = OUTDIR / f"{table}-{ts}.jsonl.gz"
            with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as fh:
                for r in rows:
                    fh.write(json.dumps(dict(r), default=str) + "\n")
            size = path.stat().st_size
            total_out += size
            manifest["tables"][table] = {"rows": len(rows),
                                         "file": path.name,
                                         "bytes": size}
            print(f"  {table:<24} {len(rows):>7,} rows -> "
                  f"{size/1e6:>7.2f} MB")
        except Exception as e:  # noqa: BLE001
            print(f"  {table:<24} FAILED: {type(e).__name__}: {e}")
            manifest["tables"][table] = {"error": str(e)}

    # schema so the DB can be recreated.
    # EXCLUDE:
    #   sqlite_%   — "CREATE TABLE sqlite_sequence" fails with "object name
    #                reserved for internal use"; SQLite creates it automatically.
    #
    # The FTS shadow tables (messages_fts_data / _content / _idx / _docsize and
    # the trigram equivalents) must NOT be excluded wholesale. Dropping anything
    # matching '%_fts_%' also deletes the virtual table DECLARATIONS themselves
    # (messages_fts, messages_fts_trigram), which are real objects we need.
    # Enumerated explicitly instead.
    SHADOW = ("messages_fts_data", "messages_fts_idx", "messages_fts_content",
              "messages_fts_docsize", "messages_fts_config",
              "messages_fts_trigram_data", "messages_fts_trigram_idx",
              "messages_fts_trigram_content", "messages_fts_trigram_docsize",
              "messages_fts_trigram_config")
    placeholders = ",".join("?" * len(SHADOW))
    schema = [r[0] for r in con.execute(
        f"SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
        f"AND name NOT LIKE 'sqlite_%' AND name NOT IN ({placeholders})",
        SHADOW)]
    (OUTDIR / f"schema-{ts}.sql").write_text(";\n".join(schema) + ";\n")
    manifest["schema_objects"] = len(schema)
    manifest["has_fts_triggers"] = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
        "AND name LIKE '%_fts_%'").fetchone()[0]
    con.close()

    manifest["total_bytes"] = total_out
    (OUTDIR / f"manifest-{ts}.json").write_text(json.dumps(manifest, indent=2))
    (OUTDIR / "LATEST").write_text(ts)

    print()
    print(f"  TOTAL BACKED UP : {total_out/1e6:.2f} MB "
          f"(from a {DB.stat().st_size/1e6:.1f} MB database)")
    print(f"  directory       : {OUTDIR}")
    print(f"  timestamp       : {ts}")

    # rotate: keep 3 most recent sets
    stamps = sorted({p.name.split("-")[-1].replace(".jsonl.gz", "")
                     .replace(".json", "").replace(".sql", "")
                     for p in OUTDIR.glob("*") if p.name != "LATEST"})
    for old in stamps[:-3]:
        for p in OUTDIR.glob(f"*-{old}*"):
            p.unlink(missing_ok=True)
        print(f"  pruned old set  : {old}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"backup failed: {type(e).__name__}: {e}")
        raise SystemExit(1)
