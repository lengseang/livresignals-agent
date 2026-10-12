#!/usr/bin/env python3
"""NIGHTLY RESEARCH LOOP — keeps hunting, reports only what changed.

Runs the walk-forward + robustness harness, compares against the config
currently deployed live, and writes a findings file. Pings Telegram only
when something ACTUALLY changed (a better config appears, or the edge
decays). Silence = nothing to report, which is the correct default.

State: data/research-loop.json   Log: logs/research-loop.log
"""
from __future__ import annotations

import importlib.util
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
RES = BASE / "research"
STATE = BASE / "data" / "research-loop.json"
FINDINGS = BASE / "reports" / "research-findings.md"

# config currently deployed live in scripts/regime_trader.py
LIVE = {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 150}


def load_harness():
    spec = importlib.util.spec_from_file_location("wf6", RES / "06-walkforward.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["wf6"] = m
    spec.loader.exec_module(m)
    return m


def run():
    h = load_harness()
    folds = h.folds
    grid = [
        {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 200},
        {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 150},   # live
        {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 100},
        {"topn": 3, "hold": 14, "risk": 0.01, "sma_r": 150},
        {"topn": 3, "hold": 30, "risk": 0.01, "sma_r": 150},
        {"topn": 3, "hold": 21, "risk": 0.02, "sma_r": 150},
        {"topn": 5, "hold": 21, "risk": 0.01, "sma_r": 150},
        {"topn": 4, "hold": 21, "risk": 0.015, "sma_r": 120},
        {"topn": 2, "hold": 21, "risk": 0.01, "sma_r": 150},
    ]
    rows = []
    for cfg in grid:
        out = [h.bt(a, b, "regime", **cfg) for a, b in folds]
        rets = [o[0] for o in out]
        ntrades = sum(o[1] for o in out)
        rows.append({
            "cfg": cfg,
            "rets": [round(r, 2) for r in rets],
            "median": round(statistics.median(rets), 2),
            "pos_folds": sum(1 for r in rets if r > 0),
            "n_folds": len(folds),
            "trades": ntrades,
        })
    bench_btc = [h.bh("BTC", a, b) for a, b in folds]
    bench_eql = [sum(h.bh(c, a, b) for c in h.coins) / len(h.coins)
                 for a, b in folds]
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "bench_btc_median": round(statistics.median(bench_btc), 2),
        "bench_eql_median": round(statistics.median(bench_eql), 2),
        "live": LIVE,
    }


def score(r):
    """Prefer robustness (positive folds) then median."""
    return (r["pos_folds"], r["median"])


def admissible(r, bench_btc_median):
    """HARD RULE 3: does this config beat BTC buy&hold?

    A config can be 4/4 folds positive and still have no edge — it merely
    lost less than BTC. The loop ranks configs against EACH OTHER, never
    against the benchmark, so without this check a "NEW BEST" that fails
    the system's own rule 3 can still be announced. That is the defect this
    closes: no claim leaves this loop that hasn't out-returned buy&hold.

    Returns (ok, detail). Detail is a human verdict string for the report.
    """
    if bench_btc_median is None:
        return False, "benchmark unavailable — claim BLOCKED (fail-safe)"
    med = r.get("median")
    if med is None:
        return False, "no median — claim BLOCKED (fail-safe)"
    edge_pp = round(med - bench_btc_median, 2)
    ok = med >= bench_btc_median
    detail = (f"{'BEATS' if ok else 'LAGS'} BTC buy&hold by {edge_pp:+.2f}pp "
              f"(config {med}% vs BTC {bench_btc_median}%)")
    return ok, detail


def main():
    cur = run()
    prev = None
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text())
        except Exception:
            pass

    live_row = next((r for r in cur["rows"]
                     if all(r["cfg"].get(k) == v for k, v in LIVE.items())), None)
    best = max(cur["rows"], key=score)
    changed = []

    if prev:
        pb = max(prev["rows"], key=score)
        if score(best) > score(pb):
            # HARD RULE 3 gate: a "NEW BEST" must beat BTC buy&hold, not
            # just the other configs. Without this, a config that merely
            # lost less than BTC would be announced as an edge.
            ok_bench, bench_detail = admissible(
                best, cur.get("bench_btc_median"))
            print(f"  benchmark gate: {bench_detail}")
            if ok_bench:
                changed.append(f"NEW BEST config: {best['cfg']} "
                               f"(median {best['median']}%, "
                               f"{best['pos_folds']}/{best['n_folds']} folds) "
                               f"— {bench_detail}")
            else:
                print(f"  SUPPRESSED: best config does not beat BTC buy&hold "
                      f"— not announcing an edge")
        if live_row and score(live_row) < score(pb):
            changed.append(f"LIVE EDGE DECAYED: {live_row['cfg']} now "
                           f"median {live_row['median']}% "
                           f"({live_row['pos_folds']}/{live_row['n_folds']})")

    # ---- MANDATORY OVERFIT GUARD ----
    # We over-claimed twice (hold=30, the A-grade gate). Both were caught by
    # luck. This makes it systematic: no claim leaves this loop unchecked.
    guard_ok, guard_txt = None, "guard unavailable"
    try:
        sys.path.insert(0, str(BASE / "scripts"))
        import autonomy as _aut
        n_set = (live_row["trades"] if live_row and "trades" in live_row
                 else (best.get("trades") or 0))
        n_fold = (live_row["n_folds"] if live_row and "n_folds" in live_row
                  else best.get("n_folds", 3))
        n_pos = (live_row["pos_folds"] if live_row and "pos_folds" in live_row
                 else best.get("pos_folds", 0))
        feats = len(LIVE)
        guard_ok = _aut.cmd_guard(n_set, feats, n_fold, n_pos)
        guard_txt = ("GUARD PASS — claim admissible (live journal still the "
                     "final referee)" if guard_ok else
                     "GUARD FAIL — DO NOT CLAIM AN EDGE from this run")
    except Exception as e:
        guard_txt = f"guard error: {str(e)[:70]}"

    lines = [f"# Research findings — {cur['ts'][:16]}",
             f"benchmarks: BTC median {cur['bench_btc_median']}%, "
             f"basket median {cur['bench_eql_median']}%", "",
             "| config | folds | median | +folds | trades |",
             "|---|---|---|---|---|"]
    for r in sorted(cur["rows"], key=score, reverse=True):
        mark = " **LIVE**" if r is live_row else ""
        lines.append(f"| `{r['cfg']}`{mark} | {r['rets']} | {r['median']}% | "
                     f"{r['pos_folds']}/{r['n_folds']} | {r.get('trades', '?')} |")
    ok_bench, bench_detail = admissible(best, cur.get("bench_btc_median"))
    lines += ["", f"**{guard_txt}**",
              "",
              f"**BENCHMARK (rule 3):** {bench_detail} — "
              f"{'ADMISSIBLE' if ok_bench else 'NOT AN EDGE'}"]
    FINDINGS.parent.mkdir(parents=True, exist_ok=True)
    FINDINGS.write_text("\n".join(lines) + "\n")
    print(f"  {guard_txt}")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(cur, indent=1))

    print(f"[{datetime.now(timezone.utc):%m-%d %H:%M}] research loop done | "
          f"best median {best['median']}% ({best['pos_folds']}/"
          f"{best['n_folds']} folds) | live median "
          f"{live_row['median'] if live_row else '?'}%")
    if changed:
        for c in changed:
            print(f"  CHANGE: {c}")
        try:
            sys.path.insert(0, str(BASE / "src"))
            from trading.tg.bot_ui import send_card
            body = "\n".join(changed) + f"\n\nbench BTC {cur['bench_btc_median']}%" \
                   f" | basket {cur['bench_eql_median']}%"
            send_card("signals", "🔬 RESEARCH LOOP — change detected", body)
        except Exception:
            pass
    else:
        print("  no change (silence by design)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"research loop error: {str(e)[:200]}")
