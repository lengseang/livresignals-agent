#!/usr/bin/env python3
"""check-books.py — validate every book's invariants and refuse to pass silently.

WHY THIS EXISTS
Each of the four defects found on 2026-09-22 was invisible to the thing that
should have caught it: a script that read a book, misread a unit, and printed a
plausible number. Nothing compared the OUTPUT against the DATA.

This is that comparison, run on a schedule. It is deliberately written to FAIL
LOUDLY rather than to be clever.

The checks, each chosen because it would have caught a real, observed failure:

  1. UNITS       every book's declared field+unit must reproduce the return
                 implied by its entry/exit prices  -> catches all three of the
                 unit errors made today
  2. SANITY      a drawdown cannot exceed 100% of peak equity, cumulative return
                 cannot be absurd  -> catches the -251% and -399% fabrications
  3. FRESHNESS   a book whose newest record is old, while the companion process
                 runs, means the process is erroring silently
  4. MONOTONIC   closed-trade counts must never DECREASE. Books are append-only;
                 a drop means a write clobbered state, which loses history
  5. STATE SHAPE flags the trend-state trap: `positions` holds the 53-symbol
                 scan universe with long=true, and real holdings live separately
                 in `real.open`. Conflating them would act on 53 positions when
                 3 are held, so the shapes are asserted, not assumed

Exit 0 = all good. Exit 1 = something is wrong and says what.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

D = Path("/opt/trading-agent/data")
NOW = dt.datetime.now(dt.timezone.utc)
fails: list[str] = []
warns: list[str] = []


def load(name):
    p = D / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:
        fails.append(f"{name}: unreadable ({e})")
        return None


def check(label, cond, detail=""):
    print(f"  {'✅' if cond else '🔴'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


def warn(label, cond, detail=""):
    if not cond:
        print(f"  ⚠️  {label}{'  ' + detail if detail else ''}")
        warns.append(label)


print("=" * 82)
print(f"BOOK HEALTH — {NOW.isoformat(timespec='seconds')}")
print("=" * 82)

# ---------------------------------------------------------------- 1. UNITS
print("\n[1] UNIT INTEGRITY (declared unit must reproduce the price-derived return)")
try:
    from book_units import as_percent, BOOKS
    CASES = [
        ("futures_executor", "futures-trades.json", "closed"),
        ("adaptive_engine", "adaptive-state.json", "closed_trades"),
        ("unified_trader", "unified-paper.json", "closed"),
        ("regime_trader", "regime-state.json", "log"),
    ]
    for book, fname, key in CASES:
        d = load(fname)
        if not d:
            continue
        recs = d.get(key) or []
        bad = 0
        for t in recs:
            e, x = t.get("entry"), t.get("exit")
            if not (isinstance(e, (int, float)) and isinstance(x, (int, float)) and e):
                continue
            want = (x - e) / e * 100
            got = as_percent(book, t)
            if got is None or abs(got - want) > 5e-3:
                bad += 1
        check(f"{book}: {len(recs)} records match declared unit", bad == 0,
              f"{bad} mismatches" if bad else "")
except Exception as e:
    fails.append(f"unit check crashed: {e}")
    print(f"  🔴 unit check crashed: {e}")

# ---------------------------------------------------------------- 2. SANITY
print("\n[2] SANITY (a drawdown cannot exceed the peak it falls from)")
try:
    from book_units import summarise
    for book, fname, key in (("futures_executor", "futures-trades.json", "closed"),
                             ("adaptive_engine", "adaptive-state.json", "closed_trades"),
                             ("unified_trader", "unified-paper.json", "closed"),
                             ("regime_trader", "regime-state.json", "log")):
        d = load(fname)
        if not d:
            continue
        s = summarise(book, d.get(key) or [])
        if not s:
            continue
        check(f"{book}: maxDD {s['max_drawdown']:.1f}% within (-100, 0]",
              -100 <= s["max_drawdown"] <= 0)
        check(f"{book}: cumulative {s['cumulative']:+.1f}% not absurd",
              abs(s["cumulative"]) < 5000)
except Exception as e:
    fails.append(f"sanity check crashed: {e}")
    print(f"  🔴 sanity check crashed: {e}")

# ------------------------------------------------------------- 3. FRESHNESS
print("\n[3] FRESHNESS (old newest-record + a running process = silent failure)")
#
# 🔴 A book at full position capacity will legitimately have NO new closes for
# as long as its hold period — regime_trader uses HOLD=21 bars at a 4h cadence,
# which is ~3.5 days, so a 5-day gap is the strategy WORKING, not failing.
#
# The first version of this check flagged regime_trader as a failure for exactly
# that reason: a false positive on a healthy, fully-invested book. A watchdog
# that cries wolf on the normal case is worse than none, because it gets ignored
# on the day it is right.
#
# So capacity is checked FIRST. A book with open positions at capacity is
# EXPECTED to be quiet, and its limit is widened to cover the hold period.
FRESH = {
    #                    key              max_h  open_field        hold_h
    "futures-trades.json": ("closed", 48, None, 0),
    "adaptive-state.json": ("closed_trades", 72, "open_positions", 24),
    "regime-state.json": ("log", 72, "positions", 4 * 21 + 12),   # HOLD=21 @4h
    "trend-state.json": ("log", 72, None, 0),
}
for fname, (key, max_h, open_field, hold_h) in FRESH.items():
    d = load(fname)
    if not d:
        continue
    recs = d.get(key) or []
    open_n = len(d.get(open_field) or []) if open_field else 0
    # generous but still finite: a book holding positions is allowed to be quiet
    effective = max_h + (hold_h if open_n else 0)
    if not recs:
        warn(f"{fname}: no records yet", False)
        continue
    stamps = []
    for t in recs:
        for k in ("close_ts_utc", "ts_utc", "ts"):
            if t.get(k):
                try:
                    stamps.append(dt.datetime.fromisoformat(t[k]))
                    break
                except Exception:
                    pass
    if not stamps:
        warn(f"{fname}: no parseable timestamps", False)
        continue
    age_h = (NOW - max(stamps)).total_seconds() / 3600
    note = f" [{open_n} open, limit widened to {effective}h]" if open_n else ""
    check(f"{fname}: newest record {age_h:.1f}h old (limit {effective}h){note}",
          age_h <= effective)

# ------------------------------------------------------------ 4. MONOTONIC
print("\n[4] MONOTONIC (books are append-only; counts must never drop)")
SNAP = D / "book-counts-snapshot.json"
counts = {}
for fname, key in (("futures-trades.json", "closed"),
                   ("adaptive-state.json", "closed_trades"),
                   ("regime-state.json", "log"),
                   ("trend-state.json", "log"),
                   ("unified-paper.json", "closed")):
    d = load(fname)
    if d:
        counts[fname] = len(d.get(key) or [])
if SNAP.exists():
    try:
        prev = json.loads(SNAP.read_text())
        for f, n in counts.items():
            p = prev.get(f)
            if p is not None:
                check(f"{f}: {p} -> {n} (never decreases)", n >= p)
    except Exception as e:
        warns.append(f"snapshot unreadable: {e}")
else:
    print("  (no snapshot yet — writing the first one)")
SNAP.write_text(json.dumps(counts, indent=1))

# ----------------------------------------------------------- 5. STATE SHAPE
print("\n[5] STATE SHAPE (the trend-state trap)")
ts = load("trend-state.json")
if ts:
    pos = ts.get("positions") or {}
    real = (ts.get("real") or {}).get("open") or []
    longs = sum(1 for v in pos.values() if v.get("long"))
    check("trend-state.positions is the SCAN UNIVERSE (long=true for all)",
          len(pos) > 0 and longs == len(pos),
          f"{longs}/{len(pos)} long")
    check("trend-state.real.open is the REAL holdings list (small, separate)",
          isinstance(real, list) and len(real) <= 5,
          f"{len(real)} real holdings")
    if len(real) > 5:
        fails.append("real.open has >5 entries — MAX_POS may be breached")
    # the specific confusion that would be dangerous
    check("real holdings are a SUBSET of the scan universe",
          all(any(r.get("symbol") == s for s in pos) for r in real))
    print(f"     ⚠️  {len(pos)} symbols flagged long, {len(real)} actually held.")
    print("     Any code treating `positions` as holdings would act on "
          f"{len(pos)} instead of {len(real)}.")

# ------------------------------------------------------------------ 6. GOLD
print("\n[6] GOLD SIGNALS (censoring fix: timeouts must not vanish)")
GOLDS = Path("/opt/trading-agent/scripts/data/gold-signals.json")
if GOLDS.exists():
    g = json.loads(GOLDS.read_text())
    st = {}
    for s in g:
        st[s.get("status")] = st.get(s.get("status"), 0) + 1
    print(f"     states: " + "  ".join(f"{k}={v}" for k, v in sorted(st.items())))
    check("no signal left in a status the resolver will ignore",
          all(k in ("OPEN", "TRACKING", "WIN", "LOSS", "TIMEOUT-EXPIRED",
                    "TIMEOUT") for k in st),
          f"unexpected: {[k for k in st if k not in ('OPEN','TRACKING','WIN','LOSS','TIMEOUT-EXPIRED','TIMEOUT')]}")
    # a TRACKING signal must have a timed_out_at stamp, else it is not being
    # followed and the censoring fix is not actually engaging
    tr = [s for s in g if s.get("status") == "TRACKING"]
    check("every TRACKING signal carries timed_out_at",
          all(s.get("timed_out_at") for s in tr),
          f"{len(tr)} tracking")
    print(f"     ⚠️  TIMEOUT is the OLD terminal state. Any signal still showing it")
    print(f"        predates the censoring fix and its true outcome is unknown.")
    n_old = st.get("TIMEOUT", 0)
    if n_old:
        print(f"        found {n_old} such signal(s).")
else:
    warn("gold-signals.json not found", False)

# ------------------------------------------------- 7. OFF-BOX BACKUP HEALTH
print("\n[7] OFF-BOX BACKUP (a backup nobody verifies is a backup that is dead)")
try:
    import subprocess
    mirror = Path("/home/ubuntu/ledger-mirror")
    if not (mirror / ".git").exists():
        fails.append("off-box ledger mirror is MISSING")
        print("  🔴 mirror directory does not exist")
    else:
        # ⚠️ git exits 0 even when it cannot reach the remote, so the exit code
        # is worthless here. Check for actual ref output instead — that is the
        # lesson from this script's own history: the replication was dead for
        # five days while every check that trusted an exit code said "fine".
        r = subprocess.run(["git", "ls-remote", "--heads", "origin"],
                           cwd=str(mirror), capture_output=True, text=True,
                           timeout=60,
                           env={**__import__("os").environ,
                                "GIT_SSH_COMMAND":
                                "ssh -i /home/ubuntu/.ssh/id_ed25519_ledger_backup "
                                "-o IdentitiesOnly=yes "
                                "-o StrictHostKeyChecking=accept-new"})
        out = (r.stdout or "") + (r.stderr or "")
        reachable = bool(r.stdout.strip()) and "ERROR" not in out.upper() \
            and "fatal" not in out.lower()
        check("off-box ledger remote is reachable", reachable,
              "" if reachable else out.strip()[:120])

        # is the off-box copy actually CURRENT, or merely reachable?
        local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(mirror),
                               capture_output=True, text=True, timeout=30)
        lh = local.stdout.strip()
        rh = r.stdout.split()[0] if r.stdout.split() else ""
        check("off-box copy is at the same commit as local",
              bool(lh) and lh == rh,
              f"local {lh[:8]} vs remote {rh[:8]}" if lh != rh else "")
        # and does it have as many records as the local ledger?
        led = Path("/opt/trading-agent/data/signal-ledger.jsonl")
        mir = mirror / "signal-ledger.jsonl"
        if led.exists() and mir.exists():
            ln = sum(1 for _ in led.open())
            mn = sum(1 for _ in mir.open())
            check(f"mirror record count matches ledger", mn == ln,
                  f"ledger {ln} vs mirror {mn}" if mn != ln else f"{ln} records")
except Exception as e:
    warn(f"off-box backup check could not run: {str(e)[:90]}")

# ------------------------------------------- 8. AGENT CODE BACKUP (off-box)
print("\n[8] AGENT CODE BACKUP (the source must not live on one disk only)")
try:
    import subprocess as _sp
    _repo = "/opt/trading-agent"
    _key = "/home/ubuntu/.ssh/id_ed25519_agent_backup"
    _remote = "git@github.com:lengseang/livresignals-agent.git"
    _env = {**__import__("os").environ,
            "GIT_SSH_COMMAND": f"ssh -i {_key} -o IdentitiesOnly=yes "
                               f"-o StrictHostKeyChecking=accept-new"}
    if not Path(_key).exists():
        fails.append("agent-backup deploy key missing")
        print("  🔴 deploy key not present")
    else:
        r = _sp.run(["git", "ls-remote", "--heads", _remote], cwd=_repo,
                    capture_output=True, text=True, timeout=60, env=_env)
        out = (r.stdout or "") + (r.stderr or "")
        # again: git exits 0 on auth failure, so judge on OUTPUT
        reach = bool(r.stdout.strip()) and "ERROR" not in out.upper() \
            and "fatal" not in out.lower()
        check("agent backup remote reachable", reach,
              "" if reach else out.strip()[:110])
        lh = _sp.run(["git", "rev-parse", "HEAD"], cwd=_repo,
                     capture_output=True, text=True, timeout=30).stdout.strip()
        m = _sp.run(["git", "ls-remote", _remote, "main"], cwd=_repo,
                    capture_output=True, text=True, timeout=60,
                    env=_env).stdout.split()
        rh = m[0] if m else ""
        check("off-box code copy matches local HEAD", bool(lh) and lh == rh,
              f"local {lh[:8]} vs remote {rh[:8]}" if lh != rh else "")
        # secrets must never be tracked
        envf = _sp.run(["git", "ls-files"], cwd=_repo, capture_output=True,
                       text=True, timeout=30).stdout.split()
        bad = [f for f in envf
               if f.endswith(".env") or "/.env." in f
               and not f.endswith(".example")]
        check("no credential file is tracked by git", not bad,
              f"{bad}" if bad else f"{len(envf)} files tracked")
except Exception as e:
    warn(f"agent code backup check could not run: {str(e)[:90]}")

print()
print("=" * 82)
if fails:
    print(f"🔴 {len(fails)} FAILURE(S):")
    for f in fails:
        print(f"   - {f}")
elif warns:
    print(f"⚠️  {len(warns)} warning(s), no failures:")
    for w in warns:
        print(f"   - {w}")
else:
    print("✅ all book-health checks pass")
print("=" * 82)
sys.exit(1 if fails else 0)
