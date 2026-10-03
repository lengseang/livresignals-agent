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
    # trend-state keeps its REAL positions under `real.open`, NOT a top-level
    # "open" field. The first version looked for a top-level one, found nothing,
    # and therefore never widened the limit — so a healthy, fully-invested
    # 3-position book was reported as stale. Dotted paths are supported now.
    "trend-state.json": ("log", 72, "real.open", 4 * 21 + 12),
}

# Log files that prove the PROCESS is still running even when it has no new
# records to report.
#
# ⚠️ "no new trades" and "not running" are different conditions and must not
# produce the same verdict. A bot that is alive and holding positions is
# healthy; a bot that stopped writing its log is broken. Without this
# distinction the freshness check flags every quiet, fully-invested book as a
# FAILURE — and a watchdog that cries wolf on the normal case gets ignored on
# the day it is right.
LIVE = {
    "adaptive-state.json": "logs/adaptive-engine.log",
    "regime-state.json": "logs/regime.log",
    "trend-state.json": "logs/trend.log",
}


def _dig(d, path):
    """Follow a dotted path into nested dicts; None if any link is missing."""
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


for fname, (key, max_h, open_field, hold_h) in FRESH.items():
    d = load(fname)
    if not d:
        continue
    recs = d.get(key) or []
    open_n = len(_dig(d, open_field) or []) if open_field else 0
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
    ok = age_h <= effective
    if not ok:
        # before failing, ask whether the process is demonstrably still alive
        rel = LIVE.get(fname)
        lp = (D.parent / rel) if rel else None
        if lp and lp.exists():
            lh = (NOW - dt.datetime.fromtimestamp(
                lp.stat().st_mtime, dt.timezone.utc)).total_seconds() / 3600
            if lh <= effective:
                warn(f"{fname}: no new records for {age_h:.1f}h, but the process"
                     f" is ALIVE (log written {lh:.1f}h ago) — idle/holding,"
                     f" not broken", False)
                continue
    check(f"{fname}: newest record {age_h:.1f}h old (limit {effective}h){note}",
          ok)

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
    warn(f"off-box backup check could not run: {str(e)[:90]}", False)

# ------------------------------------------- 8. AGENT CODE BACKUP (off-box)
print("\n[8] AGENT CODE BACKUP (the source must not live on one disk only)")
try:
    import subprocess as _sp
    _repo = "/opt/trading-agent"
    # Auth mirror of agent_backup.py: prefer GITHUB_TOKEN, fall back to ssh key.
    # (2026-09-27: the per-repo deploy key was not copied in the HKG migration,
    # so this check failed — and the backups it guards were dead too.)
    _envf = Path(__import__("os").environ.get("HERMES_HOME",
                                              "/home/ubuntu/hermes-home")) / ".env"
    _tok = ""
    if _envf.exists():
        for _l in _envf.read_text(encoding="utf-8", errors="ignore").splitlines():
            if _l.startswith("GITHUB_TOKEN="):
                _tok = _l.split("=", 1)[1].strip().strip('"').strip("'")
                break
    _key = "/home/ubuntu/.ssh/id_ed25519_agent_backup"
    _remote = ("https://x-access-token:" + _tok + "@github.com/"
               "lengseang/livresignals-agent.git") if _tok \
        else "git@github.com:lengseang/livresignals-agent.git"
    _env = {**__import__("os").environ}
    if not _tok:
        _env["GIT_SSH_COMMAND"] = (f"ssh -i {_key} -o IdentitiesOnly=yes "
                                   f"-o StrictHostKeyChecking=accept-new")
    if not _tok and not Path(_key).exists():
        fails.append("agent-backup deploy key missing and no GITHUB_TOKEN")
        print("  🔴 no usable auth for agent backup")
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
    warn(f"agent code backup check could not run: {str(e)[:90]}", False)

# --------------------------------- 9. FEE-TOLL AWARENESS (is anyone earning?)
print("\n[9] FEE TOLL (a gross edge below the fee is a loss, not a small win)")
try:
    import subprocess as _sp2
    _r = _sp2.run(["/opt/trading-agent/venv/bin/python", "-B",
                   "/opt/trading-agent/scripts/per_symbol_net.py"],
                  capture_output=True, text=True, timeout=120,
                  cwd="/opt/trading-agent")
    _out = _r.stdout or ""
    # pull the "LOSES x.xxx%/trade" lines out of the per-symbol table
    donors = []
    for ln in _out.splitlines():
        if "LOSES" in ln and "%/trade" in ln:
            parts = ln.split()
            if parts:
                donors.append(parts[0])
    if not donors:
        check("no symbol is a net donor after fees", True)
    else:
        # NOT a failure: a net-donor symbol in a PAPER book is information, not
        # an incident. It only becomes dangerous if promoted to real money —
        # which is what check [10] of promotion_report.py now prevents by
        # requiring NET expectancy rather than a win rate.
        warn(f"{len(donors)} symbol(s) have a gross edge below the "
             f"0.1594% round-trip fee: {', '.join(donors[:6])}"
             f"{' …' if len(donors) > 6 else ''}", False)
except Exception as e:
    warn(f"fee-toll check could not run: {str(e)[:80]}", False)

# ------------------------- 10. IMPROVEMENT CONSUMPTION (applied ≠ consumed)
print("\n[10] IMPROVEMENT CONSUMPTION (was the last change actually read?)")
try:
    import subprocess as _sp3
    _r3 = _sp3.run(["/opt/trading-agent/venv/bin/python", "-B",
                    "/opt/trading-agent/scripts/check-improvement-consumed.py"],
                   capture_output=True, text=True, timeout=120,
                   cwd="/opt/trading-agent")
    _o3 = (_r3.stdout or "") + (_r3.stderr or "")
    if _r3.returncode == 0:
        line = [l for l in _o3.splitlines() if "verifiably consumed" in l
                or "agent applied nothing" in l]
        check("every applied self-improve change is verifiably consumed",
              True, line[0].strip() if line else "")
    else:
        # This IS a failure, unlike the fee-toll warning: a change recorded as
        # applied but read by nothing is how the adaptive_engine learner sat
        # inert for weeks while every report said the pass completed.
        fails.append("self-improve applied a change that nothing consumes")
        for l in _o3.splitlines():
            if l.strip().startswith("- "):
                print(f"  🔴{l}")
except Exception as e:
    warn(f"improvement-consumption check could not run: {str(e)[:80]}", False)

# ------------------- 11. SITE CLAIMS RECONCILE WITH THE LEDGER
print("\n[11] SITE CLAIMS (does every number the site renders match the ledger?)")
try:
    import subprocess as _sp4
    _r4 = _sp4.run(["/opt/trading-agent/venv/bin/python", "-B",
                    "/opt/trading-agent/scripts/audit_site_claims.py"],
                   capture_output=True, text=True, timeout=120)
    _o4 = (_r4.stdout or "") + (_r4.stderr or "")
    if _r4.returncode == 0:
        line = [l for l in _o4.splitlines() if "reconcile with the ledger" in l]
        check("every site claim reconciles with the ledger", True,
              line[0].strip() if line else "")
    else:
        # A site that prints a number the ledger does not support is the one
        # failure this product cannot carry, so this is a FAILURE not a warning.
        fails.append("the public site renders a claim the ledger does not support")
        for l in _o4.splitlines():
            if l.strip().startswith("- "):
                print(f"  🔴{l}")
except Exception as e:
    warn(f"site-claim audit could not run: {str(e)[:80]}", False)

# ------------ 12. LIVE EXECUTOR LIVENESS (a silent dead real-money path)
# Added 2026-10-03 by the autonomous improvement agent.
#
# WHY: the live spot executor had not opened OR closed a single real trade for
# 24 days (newest live-trades.json record 2026-09-09) while the paper book kept
# trading every few minutes. unified.log was full of
#   "real skipped: insufficient free (0.03 < 6.0)"
# because ~86% of the ~$20.68 real equity had become stuck in NEAR/ARB (never
# sold, and NOT in live_executor.py ALLOWED_REAL so they can never be rotated),
# leaving $0.03 free USDT. Every other check passed: books were internally
# consistent, the paper path was healthy — so nothing in the fleet noticed that
# REAL trading was completely halted. This section is that oversight.
#
# It is deliberately LOCAL-ONLY (reads state files + the cached balance, no
# exchange call) so it cannot hang or rate-limit the scheduled health run.
print("\n[12] LIVE EXECUTOR LIVENESS (is real money actually trading?)")
try:
    live_on = Path("/opt/trading-agent/config/LIVE_ENABLED").exists()
    lt = load("live-trades.json") or {}
    lclosed = lt.get("closed") or []
    lopen = lt.get("open") or []
    newest_live = None
    for t in lclosed:
        s = t.get("close_ts_utc") or t.get("open_ts_utc")
        if not s:
            continue
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if newest_live is None or d > newest_live:
                newest_live = d
        except Exception:
            continue
    if live_on and newest_live is not None:
        age_d = (NOW - newest_live).total_seconds() / 86400
        # is the paper book still alive? (a dead live path with a live paper path
        # is the exact silent-failure signature; both dead = a separate problem)
        up = load("unified-paper.json") or {}
        p_newest = None
        for t in (up.get("closed") or []):
            s = t.get("ts_utc")
            if not s:
                continue
            try:
                d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
                if p_newest is None or d > p_newest:
                    p_newest = d
            except Exception:
                continue
        paper_alive = p_newest is not None and (NOW - p_newest).total_seconds() < 3 * 86400
        check("real-money path traded within the last 7 days",
              age_d <= 7,
              f"newest live closed trade {age_d:.1f}d ago" if age_d > 7 else "")
        if age_d > 7 and paper_alive:
            # diagnose the usual cause from the CACHED balance (may be stale)
            lb = load("live-balance-cache.json") or {}
            free = lb.get("usdt")
            tot = lb.get("total_usdt")
            fails.append(
                "live spot executor is SILENTLY DEAD "
                f"(no real trade in {age_d:.0f}d) while paper keeps trading — "
                f"check untradeable bag + free USDT (cached free={free})")
            print(f"     ⚠️  live-trades frozen {age_d:.0f}d while paper traded "
                  f"{0 if p_newest is None else (NOW - p_newest).total_seconds()/3600:.1f}h ago.")
            print(f"     cached balance: usdt={free} total_usdt={tot} "
                  f"(free < LIVE_MIN_FREE_BUFFER blocks every order)")
    elif not live_on:
        warn("LIVE_ENABLED flag is absent — real trading intentionally off", False)
except Exception as e:
    warn(f"live-liveness check could not run: {str(e)[:80]}", False)

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
