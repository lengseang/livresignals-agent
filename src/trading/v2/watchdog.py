"""Watchdog for the V2 paper service (cron, every 5 minutes; plus --daily for a digest). It only READS the runner's
output and ALERTS; it cannot start, stop, kill or change anything.

Conditions (alert on the transition into a problem, and once more when it clears):
  runner_silent       heartbeat.json older than SILENT_S (the runner writes it every minute)
  kill_engaged        the kill switch latched (reasons included)
  jev_failing         >= 10 entry calls and fewer than half answered by Jev (the gateway is carrying the load)
  feed_failing        feed failures grew by >= 5 since the last run
  disk_low            < 2 GB free
  chain_broken        a hash chain does not verify
  cycle_errors        cycle_error count grew
State lives in <out>/watchdog_state.json. Exit code is always 0 (cron must not flap); problems are in the output.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Optional

from trading.v2.notify import Notifier

SILENT_S = 300
STALE_RATE = 0.15
STALE_SYMBOL_RATE = 0.5
MIN_FREE_GB = 2.0


def assess(out: Path, now: float, prev: dict) -> tuple[dict, dict]:
    """(problems {name: detail}, counters to remember). Pure apart from reading files."""
    problems: dict[str, str] = {}
    hb_path = out / "heartbeat.json"
    hb = None
    if not hb_path.exists():
        problems["runner_silent"] = "no heartbeat.json yet"
    else:
        age = now - hb_path.stat().st_mtime
        try:
            hb = json.loads(hb_path.read_text())
        except Exception:
            problems["runner_silent"] = "heartbeat.json unreadable"
        if age > SILENT_S:
            problems["runner_silent"] = f"heartbeat {age:.0f}s old"
    counters = dict(prev.get("counters", {}))
    if hb:
        if hb.get("kill_engaged"):
            problems["kill_engaged"] = "reasons: " + "; ".join(hb.get("kill_reasons", []))[:300]
        calls, ok = hb.get("jev_calls", 0) or 0, hb.get("jev_ok", 0) or 0
        if calls >= 10 and ok < calls / 2:
            problems["jev_failing"] = f"Jev answered {ok}/{calls} entry calls; gateway is answering"
        ff = hb.get("feed_failures", 0) or 0
        if ff - counters.get("feed_failures", ff) >= 5:
            problems["feed_failing"] = f"feed failures {counters.get('feed_failures')} -> {ff}"
        counters["feed_failures"] = ff
        bud = hb.get("jev_budget")
        if isinstance(bud, dict) and bud.get("cap") and bud.get("used", 0) >= 0.9 * bud["cap"]:
            problems["jev_budget"] = (f"Jev calls today {bud['used']}/{bud['cap']}; refused {bud.get('refused', 0)} "
                                      "(over the cap the gateway answers)")
        by = hb.get("by_symbol") or {}
        cyc = sum(v.get("cycles", 0) for v in by.values())
        bad = {k: v["unusable"] for k, v in by.items() if v.get("unusable")}
        # judged only after >= 8 decision boundaries (the one unusable boundary right after a restart is expected):
        #   overall > 15% skipped as stale, OR any single coin skipped more than half the time (with 15 coins one dead
        #   coin is only 6.7% overall and would otherwise never alert)
        sick = {k: v for k, v in by.items() if v.get("cycles", 0) >= 8 and v.get("unusable", 0) / v["cycles"] > STALE_SYMBOL_RATE}
        overall = by and cyc >= 8 * len(by) and sum(bad.values()) / cyc > STALE_RATE
        if overall or sick:
            worst = ", ".join(f"{k}:{n}" for k, n in sorted(bad.items(), key=lambda kv: -kv[1])[:5])
            problems["stale_snapshots"] = (f"{sum(bad.values())}/{cyc} decisions skipped as stale ({worst}); "
                                           f"step {hb.get('timing', {}).get('step_ms_last')} ms")
        ce = (hb.get("outcomes") or {}).get("cycle_error", 0)
        if ce > counters.get("cycle_error", 0):
            problems["cycle_errors"] = f"cycle errors {counters.get('cycle_error', 0)} -> {ce}"
        counters["cycle_error"] = ce
    free = shutil.disk_usage(out if out.exists() else out.parent).free / 1e9
    if free < MIN_FREE_GB:
        problems["disk_low"] = f"{free:.1f} GB free"
    from trading.shadow.chain import verify
    for lane, strat in (("v2cycle", "pipeline"), ("v2paper", "book"), ("v2kill", "controller")):
        d = out / lane / strat
        if d.exists():
            v = verify(d)
            if not v["ok"]:
                problems["chain_broken"] = f"{lane}/{strat}: {v['error']}"
    return problems, counters


def digest(out: Path) -> str:
    try:
        hb = json.loads((out / "heartbeat.json").read_text())
    except Exception:
        return "no heartbeat"
    lines = [f"equity ${hb.get('paper_equity', 0):,.2f} | positions {hb.get('positions')} | cycles {hb.get('cycles')}",
             f"outcomes {hb.get('outcomes')}",
             f"jev {hb.get('jev_ok')}/{hb.get('jev_calls')} | gateway takeovers {hb.get('fallback_gateway')} | "
             f"dual-run {hb.get('dual_run')} | advice {hb.get('advice')}",
             f"exits {hb.get('exits')} | stops tightened {hb.get('stops_tightened')} | kill {hb.get('kill_engaged')}"]
    sc = out / "scorecard.json"
    if sc.exists():
        try:
            s = json.loads(sc.read_text())
            for src, v in s.get("sources", {}).items():
                pl = v.get("p_long") or {}
                lines.append(f"score {src}: n={v.get('decisions')} graded={v.get('graded_barrier')} "
                             f"hit24h={v.get('directional_hit_rate_24h')} brier={pl.get('brier')} base={v.get('brier_base_rate')}")
            for src, a in s.get("position_advice", {}).items():
                lines.append(f"advice {src}: exits={a.get('exits')} exit_precision={a.get('exit_precision')}"
                             + (" -> E13 REVIEW: exits beat holding < 50%; consider EXIT/REDUCE >= 0.80"
                                if a.get("e13_review") else ""))
        except Exception:
            lines.append("scorecard unreadable")
    return "\n".join(lines)


def run(out: Path, notifier: Notifier, now: Optional[float] = None, daily: bool = False) -> dict:
    now = time.time() if now is None else now
    state_path = out / "watchdog_state.json"
    try:
        prev = json.loads(state_path.read_text()) if state_path.exists() else {}
    except Exception:
        prev = {}
    problems, counters = assess(out, now, prev)
    # "alerted" = problems whose alert was actually DELIVERED. A failed send is retried next run, never forgotten
    # (audit V2-16: the first version marked a problem as reported even when Telegram was unreachable).
    alerted = set(prev.get("alerted", prev.get("active", [])))
    sent = []
    for name, detail in problems.items():
        if name not in alerted and notifier.send(f"[V2 paper] PROBLEM {name}", detail):
            alerted.add(name)
            sent.append(name)
    for name in sorted(alerted - set(problems)):
        if notifier.send(f"[V2 paper] recovered: {name}", "condition cleared"):
            alerted.discard(name)
            sent.append(f"recovered:{name}")
    if daily and notifier.send("[V2 paper] daily digest", digest(out)):
        sent.append("digest")
    out.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"active": sorted(problems), "alerted": sorted(alerted), "counters": counters,
                                      "ts": now}))
    return {"problems": problems, "sent": sent, "undelivered": sorted(set(problems) - alerted)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--daily", action="store_true", help="also send the daily digest")
    a = ap.parse_args(argv)
    r = run(a.out, Notifier(a.out / "notify_state.json"), daily=a.daily)
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
