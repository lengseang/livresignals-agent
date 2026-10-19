"""Fragility probe: is the sma_r=200 "NEW BEST" a property of the CONFIG or of one coin?

For each candidate sma_r, compare its full-universe walk-forward median against the
same config on 25 leave-one-coin-out universes. If the edge to the live config is
smaller than the leave-one-out spread, the ranking is coin luck, not a parameter.
"""
import contextlib
import importlib.util
import io
import random
import statistics
import sys

spec = importlib.util.spec_from_file_location("wf6", "/opt/trading-agent/research/06-walkforward.py")
m = importlib.util.module_from_spec(spec)
with contextlib.redirect_stdout(io.StringIO()):
    spec.loader.exec_module(m)

BASE = [c for c in m.coins if c != "BTC"]


def bt_sub(a, b, sma_r, sub):
    old = m.coins
    m.coins = sub
    try:
        return m.bt(a, b, "regime", topn=3, hold=21, risk=0.01, sma_r=sma_r)[0]
    finally:
        m.coins = old


def probe(sma_r, n_draws, seed=0):
    full = [m.bt(a, b, "regime", topn=3, hold=21, risk=0.01, sma_r=sma_r)[0]
            for a, b in m.folds]
    med = statistics.median(full)
    diffs, meds = [], []
    for i in range(n_draws):
        sub = BASE[:]
        random.Random(seed + i).shuffle(sub)
        sub = sorted(sub[:len(sub) - 1])
        rr = [bt_sub(a, b, sma_r, sub) for a, b in m.folds]
        meds.append(statistics.median(rr))
        diffs.append(med - statistics.median(rr))
    return {
        "sma_r": sma_r,
        "folds": [round(x, 2) for x in full],
        "median": round(med, 2),
        "pos": sum(1 for x in full if x > 0),
        "loo_median": round(statistics.median(meds), 2),
        "loo_min": round(min(meds), 2),
        "loo_max": round(max(meds), 2),
        "loo_drop_mean": round(statistics.mean(diffs), 2),
        "loo_drop_max": round(max(diffs), 2),
        "draws": n_draws,
    }


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    rows = [probe(sr, n) for sr in (200, 100, 150)]
    import json
    print(json.dumps(rows, indent=1))
