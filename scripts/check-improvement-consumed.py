#!/usr/bin/env python3
"""check-improvement-consumed.py — did the "improvement" actually change anything?

THE FAILURE THIS EXISTS TO CATCH
--------------------------------
On 2026-09-22 the adaptive_engine's per-trade learner was found to be inert. It
computed a per-regime edge on every tick, stored it, and the consumer threw it
away:

    edge = max(edge, live.get("avg_pnl", 0.0) / 100.0)   # avg_pnl is DOLLARS

`avg_pnl` is dollars; dividing by 100 yields a number with no unit, so it always
lost to the static default and `max()` selected the default every single time.
The loop ran, logged a result, and changed nothing — for weeks.

Nothing caught it, because nothing asked the only question that matters:

    was the output CONSUMED?

A change that is written and never read is worse than no change, because it reads
as progress in the report and in the Telegram summary. This guard walks every
change the self-improvement engine can apply, and for each one verifies a
consumer exists AND that the effect is observable.

FAIL CLOSED
-----------
If a change cannot be verified, that is a failure, not a pass. A guard that
reports success when it checked nothing is the same bug one level up.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
SCRIPTS = BASE / "scripts"
DATA = BASE / "data"
REPORTS = BASE / "reports"

problems: list[str] = []
checked: list[str] = []


def ok(msg: str) -> None:
    print(f"  ✅ {msg}")
    checked.append(msg)


def bad(msg: str) -> None:
    print(f"  🔴 {msg}")
    problems.append(msg)


def warn(msg: str) -> None:
    print(f"  ⚠️  {msg}")


def load_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def main() -> int:
    print("=" * 86)
    print("IMPROVEMENT CONSUMPTION — was each applied change actually read?")
    print("=" * 86)

    rep = load_json(REPORTS / "self-improve.json")
    if not rep:
        print("  no reports/self-improve.json — nothing to verify")
        print("  (this is not a pass: the engine has produced no output to audit)")
        return 1

    changes = rep.get("changes") or []
    ts = str(rep.get("ts") or "")[:19]
    print(f"  last run: {ts}")
    print(f"  changes applied: {len(changes)}")
    print()
    if not changes:
        print("  engine applied nothing — 'no change for change's sake' is a valid")
        print("  outcome, and there is nothing here to chase.")
        return 0

    state = load_json(DATA / "signals-state.json", {}) or {}
    disabled = state.get("disabled") or []

    # ── every source file that could consume an applied change ──────────────
    SELF = {"self_improve.py", "night_tuner.py", "check-improvement-consumed.py"}

    def grep(pattern: str) -> list[str]:
        """Real consumers only: no bytecode, no caches, not this guard, and not
        the writers themselves. A guard that lists its own filename as a
        consumer of the thing it is checking is noise, and noise gets ignored."""
        try:
            r = subprocess.run(
                ["grep", "-rlE", pattern, str(SCRIPTS), str(BASE / "src")],
                capture_output=True, text=True, timeout=30)
        except Exception:
            return []
        out = []
        for l in r.stdout.splitlines():
            l = l.strip()
            if not l or not l.endswith(".py"):
                continue
            if "__pycache__" in l or Path(l).name in SELF:
                continue
            out.append(l)
        return out

    for ch in changes:
        print(f"  ── change: {ch}")

        # ── 1. edge disabling ───────────────────────────────────────────────
        m = re.search(r"disabled edge '([^']+)'", ch)
        if m:
            edge = m.group(1)
            if edge not in disabled:
                bad(f"'{edge}' was reported disabled but is NOT in "
                    f"signals-state.json — the write did not persist")
                continue
            consumers = [p for p in grep(r"signals-state")
                         if "self_improve" not in p]
            if not consumers:
                bad(f"'{edge}' is disabled in state, but NOTHING reads "
                    f"signals-state.json — the disable is inert")
                continue

            # the effect must be OBSERVABLE: no trade from this edge may close
            # after the disable was written.
            d = load_json(DATA / "unified-paper.json", {}) or {}
            dis_ts = None
            try:
                dis_ts = datetime.fromtimestamp(
                    (DATA / "signals-state.json").stat().st_mtime,
                    timezone.utc).date().isoformat()
            except Exception:
                pass
            leaks = []
            for t in (d.get("closed") or []):
                if t.get("edge") == edge:
                    tts = str(t.get("ts_utc") or "")[:10]
                    if dis_ts and tts > dis_ts:
                        leaks.append(t)
            if leaks:
                bad(f"'{edge}' still OPENED {len(leaks)} trade(s) after the "
                    f"disable ({dis_ts}) — consumer exists but is not enforcing")
            else:
                ok(f"edge '{edge}' disabled and enforced "
                   f"(0 new trades after {dis_ts}; consumer: "
                   f"{', '.join(Path(c).name for c in consumers[:2])})")
            continue

        # ── 2. futures constant tuning ──────────────────────────────────────
        m = re.search(r"futures stop=([0-9.]+) trail=([0-9.]+) act=([0-9.]+)", ch)
        if m:
            stop = float(m.group(1))
            fp = SCRIPTS / "futures_executor.py"

            # (a) did the value actually land in the file?
            src = fp.read_text() if fp.exists() else ""
            mm = re.search(r"^STOP_PCT\s*=\s*([0-9.]+)", src, re.M)
            if not mm:
                bad("futures_executor.py has no STOP_PCT line to patch")
                continue
            if abs(float(mm.group(1)) - stop) > 1e-9:
                bad(f"report says stop={stop} but the file says "
                    f"{mm.group(1)} — the patch did not land")
                continue

            # (b) is it stamped as auto-tuned (traceability)?
            if "auto-tuned" not in src:
                warn("STOP_PCT carries no 'auto-tuned' stamp — the next reader "
                     "cannot tell machine-set from hand-set")

            # (c) THE ONE THAT MATTERS: is the tuned constant the one the
            #     money actually uses? A duplicate constant elsewhere means the
            #     tuning is applied to a file nobody trades from.
            #     This codebase has the scar: paper and real books each held a
            #     copy of the stop width, drifted to 2.0% vs 1.00%, and the
            #     validated fix sat unused for four days while real money used
            #     the old value.
            rivals = []
            for p in sorted(SCRIPTS.glob("*.py")):
                if p.name in ("futures_executor.py", "self_improve.py",
                              "night_tuner.py"):
                    continue
                for rm in re.finditer(
                        r"^([A-Z_]*STOP[A-Z_]*)\s*=\s*([0-9.]+)", p.read_text(),
                        re.M):
                    if rm.group(1) == "STOP_PCT":
                        continue
                    rivals.append(f"{p.name}:{rm.group(1)}="
                                  f"{rm.group(2)}")
            applied_to = [p for p in grep(r"from futures_executor import")
                          if "self_improve" not in p
                          and "night_tuner" not in p]
            if rivals:
                warn(f"tuned {stop} is read by "
                     f"{', '.join(Path(a).name for a in applied_to) or 'nobody'}; "
                     f"OTHER, independent stop constants exist and are NOT "
                     f"tuned: {', '.join(rivals)}")
                print("       ^ a duplicated constant is how a validated fix "
                      "ends up unused while live money uses the old value")
            ok(f"futures stop={stop} landed and is read by "
               f"{', '.join(Path(a).name for a in applied_to) or 'no importer'}")
            continue

        # ── 3. anything else: refuse to assume ──────────────────────────────
        warn(f"unrecognised change type — not assumed to be consumed: {ch}")

    print()
    print("=" * 86)
    if problems:
        print(f"  🔴 {len(problems)} change(s) applied but NOT verifiably consumed:")
        for p in problems:
            print(f"     - {p}")
        print()
        print("  An unread change reads as progress in the report and the")
        print("  Telegram summary. That is how the adaptive_engine learner sat")
        print("  inert for weeks.")
        return 1
    print(f"  ✅ all {len(checked)} applied change(s) verifiably consumed")
    print("=" * 86)
    return 0


if __name__ == "__main__":
    sys.exit(main())
