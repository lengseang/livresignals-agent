#!/usr/bin/env python3
"""Prove the state lock closes the lost-update race that ate the 2026-10-08 LINK buy.

The bug: manage() read state, spent seconds on network calls, then wrote its
in-memory snapshot back. A buy completing in that window was overwritten and
became a ghost position — held on the exchange, absent from the ledger, with no
stop and not counted in max_open.

This test simulates the interleaving WITHOUT touching the exchange or the real
state file: two threads, one appending a record the slow way (read, sleep, write),
one writing its own snapshot. With the lock, both writes survive. Without it, one
is lost.

Run: python3 scripts/test_state_lock.py
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

OK = []
FAIL = []


def chk(name: str, ok: bool) -> None:
    (OK if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")


def run_case(use_lock: bool) -> dict:
    """Two writers racing on one state file. Returns the final state."""
    tmp = Path(tempfile.mkdtemp())
    state = tmp / "state.json"
    lock = tmp / "state.lock"
    state.write_text(json.dumps({"open": [], "closed": []}))

    def locked_write(mutate):
        fh = open(lock, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            s = json.loads(state.read_text())
            mutate(s)
            time.sleep(0.05)                 # the network round-trip window
            state.write_text(json.dumps(s))
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    def unlocked_write(mutate):
        s = json.loads(state.read_text())
        mutate(s)
        time.sleep(0.05)
        state.write_text(json.dumps(s))

    write = locked_write if use_lock else unlocked_write

    def buyer():
        write(lambda s: s["open"].append({"symbol": "LINK/USDT", "qty": 0.43}))

    def manager():
        # The real manage() re-reads state INSIDE the lock, then writes back only
        # the record it changed. Model that faithfully: toggling one position's
        # state inside the lock. If a test reads outside the lock and replaces the
        # whole document, it is testing a bug the real code does not have.
        if use_lock:
            def mut(st):
                st["closed"].append({"symbol": "OLD/USDT"})
            locked_write(mut)
        else:
            # unlocked: read late, mutate, write late — same window as the buyer
            s = json.loads(state.read_text())
            s["closed"].append({"symbol": "OLD/USDT"})
            time.sleep(0.05)
            state.write_text(json.dumps(s))

    t1 = threading.Thread(target=buyer)
    t2 = threading.Thread(target=manager)
    t1.start(); t2.start()
    t1.join(); t2.join()
    return json.loads(state.read_text())


print("=== state lock: does it stop the lost update? ===\n")

print("  WITHOUT the lock (reproduces the bug):")
bad = run_case(use_lock=False)
has_link_bad = any(r.get("symbol") == "LINK/USDT" for r in bad["open"])
print(f"    final open: {bad['open']}")
chk("without lock the buy is LOST (bug reproduced)", not has_link_bad)
print()

print("  WITH the lock (the fix):")
good = run_case(use_lock=True)
has_link_good = any(r.get("symbol") == "LINK/USDT" for r in good["open"])
has_old = any(r.get("symbol") == "OLD/USDT" for r in good["closed"])
print(f"    final open: {good['open']}")
print(f"    final closed: {good['closed']}")
chk("with lock the buy SURVIVES", has_link_good)
chk("with lock the manager's close SURVIVES too", has_old)

print()
if FAIL:
    print(f"  {len(FAIL)} FAILURE(S):")
    for f in FAIL:
        print(f"    - {f}")
    sys.exit(1)
print(f"  all {len(OK)} checks pass")
sys.exit(0)
