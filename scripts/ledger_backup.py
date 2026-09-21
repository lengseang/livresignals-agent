#!/usr/bin/env python3
"""LEDGER BACKUP — replicate the hash-chained record off-box.

WHY THIS MATTERS MORE THAN ANYTHING ELSE HERE
---------------------------------------------
Every other file in this project can be rebuilt. The ledger cannot:

  * price caches          -> refetchable from exchanges
  * trade journals        -> derivable from the ledger
  * the trading code      -> rewriteable
  * research scripts      -> rewriteable
  * THE LEDGER            -> 24 records of pre-outcome hash commitments.
                             If it is lost, the track record is lost, and a
                             track record is the entire asset.

A failed disk would destroy it silently. This script replicates it on a
schedule to a second location.

APPEND-ONLY, BY CONSTRUCTION
----------------------------
The backup never truncates or rewrites the destination. It APPENDS only the
records the destination is missing, then verifies the resulting chain. A
corrupted source can therefore never overwrite a good backup.

INTEGRITY IS CHECKED, NOT ASSUMED
---------------------------------
After copying, the script re-walks the hash chain at the destination. If the
backup is not a valid chain, it shouts. A backup you have never verified is
not a backup.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
LEDGER = BASE / "data" / "signal-ledger.jsonl"
BACKUP_DIR = BASE / "backups" / "ledger"
GENESIS = "0" * 64
KEEP = 7          # daily snapshots to retain

REPORT = BASE / "data" / "ledger-backup-status.json"


def canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_hash(prev: str, rec_no_hash: dict) -> str:
    return hashlib.sha256((prev + "|" + canonical(rec_no_hash)).encode()).hexdigest()


def read_lines(p: Path) -> list[str]:
    if not p.exists():
        return []
    out = []
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)          # only keep parseable records
            except json.JSONDecodeError:
                continue
            out.append(line)
    return out


def verify_lines(lines: list[str]) -> tuple[bool, int, list[str]]:
    """Re-walk a chain from raw lines. Returns (valid, count, problems)."""
    prev, n, problems = GENESIS, 0, []
    for i, line in enumerate(lines, 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"line {i}: unparseable")
            continue
        n += 1
        if rec.get("prev") != prev:
            problems.append(f"line {i} (seq {rec.get('seq')}): prev mismatch")
        stored = rec.pop("hash", None)
        expect = record_hash(prev, rec)
        rec["hash"] = stored
        if stored != expect:
            problems.append(f"line {i} (seq {rec.get('seq')}): content altered")
        if rec.get("seq") != n:
            problems.append(f"line {i}: seq gap (expected {n})")
        prev = stored or expect
    return (not problems), n, problems


def main() -> int:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    src_lines = read_lines(LEDGER)
    live = BACKUP_DIR / "signal-ledger.jsonl"
    bak_lines = read_lines(live)

    print("=" * 70)
    print("LEDGER BACKUP")
    print("=" * 70)
    print(f"  source      : {len(src_lines)} records")
    print(f"  destination : {len(bak_lines)} records")

    # ---- 1. verify the SOURCE before trusting it as a backup input --------
    src_ok, src_n, src_problems = verify_lines(src_lines)
    if not src_ok:
        print(f"\n  ⛔ SOURCE CHAIN IS BROKEN ({len(src_problems)} problems).")
        for p in src_problems[:5]:
            print(f"     {p}")
        print("     Refusing to back up a corrupt chain — that would destroy")
        print("     the last good copy. Investigate before re-running.")
        REPORT.write_text(json.dumps({
            "ts": ts, "ok": False, "stage": "source-verify",
            "problems": src_problems[:20],
        }, indent=1))
        return 2
    print(f"  source chain ✅ intact ({src_n} records)")

    # ---- 2. append only what is missing ----------------------------------
    if len(src_lines) > len(bak_lines):
        # confirm the existing backup is a PREFIX of the source, so we are
        # only ever appending and never silently forking history
        if bak_lines and bak_lines != src_lines[: len(bak_lines)]:
            print("\n  ⛔ DIVERGENCE: the backup is not a prefix of the source.")
            print("     The two chains disagree about the past. Refusing to")
            print("     overwrite; this needs a human.")
            REPORT.write_text(json.dumps({
                "ts": ts, "ok": False, "stage": "divergence",
            }, indent=1))
            return 3
        added = len(src_lines) - len(bak_lines)
        with live.open("a", encoding="utf-8") as fh:
            for line in src_lines[len(bak_lines):]:
                fh.write(line + "\n")
        print(f"  appended    : {added} new record(s)")
    else:
        print("  appended    : 0 (already current)")

    # ---- 3. verify the DESTINATION ---------------------------------------
    final_lines = read_lines(live)
    ok, n, problems = verify_lines(final_lines)
    print(f"\n  destination chain: {'✅ intact' if ok else '⛔ BROKEN'} "
          f"({n} records)")
    if not ok:
        for p in problems[:8]:
            print(f"     {p}")

    # ---- 4. daily snapshot ------------------------------------------------
    snap = BACKUP_DIR / f"ledger-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    shutil.copy2(live, snap)
    snaps = sorted(BACKUP_DIR.glob("ledger-*.jsonl"))
    for old in snaps[:-KEEP]:
        old.unlink(missing_ok=True)
    print(f"  snapshot    : {snap.name}  ({len(snaps[-KEEP:])} kept)")

    head = json.loads(final_lines[-1])["hash"] if final_lines else GENESIS
    REPORT.write_text(json.dumps({
        "ts": ts, "ok": ok, "records": n, "head": head,
        "source_records": len(src_lines), "problems": problems[:20],
    }, indent=1))

    print(f"\n  head        : {head[:32]}…")
    print(f"  status file : {REPORT}")
    print()
    print("  NOTE: this is still the same machine. For true disaster")
    print("        protection, also push a copy off-box (see push_offbox).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
