#!/usr/bin/env python3
"""CHAMPION / CHALLENGER — let the system improve its own live config.

THE IDEA: stop hand-editing the trader. Declare the deployed config the
CHAMPION. Every night, run a set of CHALLENGERS through the same
walk-forward harness. Promote a challenger ONLY if it beats the champion
on evidence that survives the overfit guard.

THE BIAS IS DELIBERATELY CONSERVATIVE. Default action = DO NOTHING.
A config gets promoted only when it is clearly better, never merely newer.
Churn is how you destroy a track record - the record IS the product.

PROMOTION RULES (all must hold):
  1. challenger PASSES the overfit guard
  2. challenger is not a previously-rejected config
  3. EITHER  more positive folds than champion
     OR      same folds AND median >= champion median * 1.15
             (15% margin - below that we are chasing noise)

DEMOTION: if the *champion* starts failing the guard (edge decay), we flag
it loudly rather than silently changing anything.

State: data/champion.json      Log: logs/champion.log
Reads/writes: data/champion.json  <- regime_trader.py reads this
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
CHAMP = BASE / "data" / "champion.json"
LOG = BASE / "logs" / "champion.log"

# the config shipped in code — used to seed champion.json on first run
SHIPPED = {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 150}
SHIPPED_V = {"TP_R": 2.0, "STOP_ATR": 2.5}

# challengers: neighbours of the champion, not a wide random sweep.
# Wide sweeps are how you overfit; neighbours are how you refine.
CHALLENGERS = [
    {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 200},
    {"topn": 3, "hold": 21, "risk": 0.01, "sma_r": 100},
    {"topn": 3, "hold": 14, "risk": 0.01, "sma_r": 150},
    {"topn": 3, "hold": 28, "risk": 0.01, "sma_r": 150},
    {"topn": 3, "hold": 21, "risk": 0.02, "sma_r": 150},
    {"topn": 3, "hold": 21, "risk": 0.015, "sma_r": 150},
    {"topn": 2, "hold": 21, "risk": 0.01, "sma_r": 150},
    {"topn": 4, "hold": 21, "risk": 0.01, "sma_r": 150},
    {"topn": 4, "hold": 21, "risk": 0.015, "sma_r": 120},
]

MARGIN = 1.15          # challenger must beat champion by 15% to justify churn


def load_harness():
    spec = importlib.util.spec_from_file_location("wf6", RES / "06-walkforward.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["wf6"] = m
    spec.loader.exec_module(m)
    return m


def load_champ():
    if CHAMP.exists():
        try:
            return json.loads(CHAMP.read_text())
        except Exception:
            pass
    return {
        "config": dict(SHIPPED),
        "variant": dict(SHIPPED_V),
        "since": datetime.now(timezone.utc).isoformat(),
        "promotions": [],
        "rejected": [],
    }


def save_champ(c):
    CHAMP.parent.mkdir(parents=True, exist_ok=True)
    CHAMP.write_text(json.dumps(c, indent=1))


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M}] {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def evaluate(h, cfg):
    out = [h.bt(a, b, "regime", **cfg) for a, b in h.folds]
    rets = [o[0] for o in out]
    return {
        "cfg": cfg,
        "rets": [round(r, 2) for r in rets],
        "median": round(statistics.median(rets), 2),
        "pos_folds": sum(1 for r in rets if r > 0),
        "n_folds": len(rets),
        "trades": sum(o[1] for o in out),
    }


def guard_ok(row, n_features=4):
    """the same guard as autonomy.py, applied to the challenger"""
    n, f, folds = row["trades"], n_features, row["n_folds"]
    need_pos = max(1, round(folds * 0.75))
    checks = {
        "sample_size": n >= 20 * f,
        "fold_count": folds >= 3,
        "fold_consistency": row["pos_folds"] >= need_pos,
        "per_fold": n >= 50 * folds,
    }
    return all(checks.values()), checks


def beat(chall, champ):
    """conservative: must be BETTER, not merely different"""
    if chall["pos_folds"] > champ["pos_folds"]:
        return True, (f"more positive folds ({chall['pos_folds']} vs "
                      f"{champ['pos_folds']})")
    if (chall["pos_folds"] == champ["pos_folds"] and
            champ["median"] != 0 and
            chall["median"] >= champ["median"] * MARGIN):
        return True, (f"same folds, median {chall['median']}% vs "
                      f"{champ['median']}% (>={MARGIN}x margin)")
    return False, (f"no clear win (folds {chall['pos_folds']} vs "
                   f"{champ['pos_folds']}, median {chall['median']}% vs "
                   f"{champ['median']}%)")


def main():
    h = load_harness()
    c = load_champ()
    champ_cfg = c["config"]

    row_c = evaluate(h, champ_cfg)
    ok_c, checks_c = guard_ok(row_c)
    log(f"CHAMPION {champ_cfg} -> median {row_c['median']}% "
        f"folds {row_c['pos_folds']}/{row_c['n_folds']} "
        f"trades {row_c['trades']} | guard {'PASS' if ok_c else 'FAIL'}")

    if not ok_c:
        log("⚠️ CHAMPION FAILS THE GUARD — edge may be decaying. "
            "NOT auto-changing anything; flagging for review.")
        log(f"    checks: {checks_c}")

    best, best_row, best_why = None, None, None
    for cfg in CHALLENGERS:
        if cfg == champ_cfg:
            continue
        if cfg in c.get("rejected", []):
            continue
        r = evaluate(h, cfg)
        ok, _ = guard_ok(r)
        if not ok:
            log(f"  challenger {cfg} -> median {r['median']}% "
                f"folds {r['pos_folds']}/{r['n_folds']} — GUARD FAIL, skipped")
            continue
        won, why = beat(r, row_c)
        log(f"  challenger {cfg} -> median {r['median']}% "
            f"folds {r['pos_folds']}/{r['n_folds']} "
            f"{'BEATS' if won else 'loses'}: {why}")
        if won and (best_row is None or r["median"] > best_row["median"]):
            best, best_row, best_why = cfg, r, why

    if best is None:
        log("RESULT: no challenger beats the champion. NO CHANGE. "
            "(this is the healthy outcome)")
        save_champ(c)
        return

    # ---- promote ----
    c["promotions"].append({
        "date": datetime.now(timezone.utc).isoformat(),
        "from": champ_cfg,
        "to": best,
        "reason": best_why,
        "champion_was": {"median": row_c["median"],
                         "folds": f"{row_c['pos_folds']}/{row_c['n_folds']}"},
        "challenger_is": {"median": best_row["median"],
                          "folds": f"{best_row['pos_folds']}/"
                                   f"{best_row['n_folds']}"},
        "trades": best_row["trades"],
    })
    c["config"] = best
    c["since"] = datetime.now(timezone.utc).isoformat()
    save_champ(c)
    log(f"✅ PROMOTED {champ_cfg} -> {best}  ({best_why})")
    log(f"    regime_trader.py picks this up on its next run")
    try:
        from trading.tg.bot_ui import send_card
        send_card("signals", "🏆 Champion promoted",
                  f"{champ_cfg}\n  ->  {best}\n\n{best_why}\n"
                  f"was: median {row_c['median']}%, "
                  f"folds {row_c['pos_folds']}/{row_c['n_folds']}\n"
                  f"now: median {best_row['median']}%, "
                  f"folds {best_row['pos_folds']}/{best_row['n_folds']}\n"
                  f"trades {best_row['trades']}")
    except Exception as e:
        log(f"  notify failed: {str(e)[:60]}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"champion job error: {str(e)[:200]}")
