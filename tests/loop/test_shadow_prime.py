"""P2b priming: history -> live equals one replay of the whole span; monthly + daily files read in order."""
from __future__ import annotations

import heapq
import json
import zipfile

import numpy as np
from test_replay import daily_minutes, ms, row

from trading.replay.broker import CostModel, PaperAccount
from trading.replay.engine import replay
from trading.replay.plugins.trend_sma import TrendSMA
from trading.scanner.bars import ticks_from_bar
from trading.shadow import prime
from trading.shadow.fleet import ShadowFleet

D0 = ms("2021-03-01T00:00:00")
DAY = 86_400_000
COST = CostModel(0.001, 2.0, 2.0)


def bars(n=60, seed=5):
    rng = np.random.default_rng(seed)
    out = {}
    for s in ("BTCUSDT", "ETHUSDT"):
        c = 100 * np.cumprod(1 + 0.03 * np.sin(np.arange(n) / 4) + 0.01 * rng.standard_normal(n))
        out[s] = daily_minutes(s, D0, c.tolist(), per_day=2)
    return out


def test_priming_then_live_equals_one_replay(tmp_path):
    B = bars()
    k = D0 + 30 * DAY                                                    # history before, live after
    fleet = ShadowFleet(["BTCUSDT", "ETHUSDT"], [{"lane": "candidate", "strategy": "trend_sma", "slots": 2,
                                                   "params": {"sma": 5, "btc_sma": 5}}],
                        tmp_path, forward_start_ms=k, costs=COST, grace_ms=None)
    prime.prime_history(fleet, lambda s: [b for b in B[s] if b.end_ms <= k])
    live = [ticks_from_bar(b) for s in B for b in B[s] if b.open_ms >= k]
    for t in heapq.merge(*live, key=lambda t: (t.ts_exchange_ms, t.symbol)):
        fleet.on_tick(t)
    fleet.finish()

    ref_acct = PaperAccount(1000.0, 2, COST)
    res = replay(B, [TrendSMA(sma=5, btc_sma=5)], {"net": ref_acct})
    want = [(e["payload"]["symbol"], e["payload"]["action"], e["ts_ms"]) for e in res.signal_events if e["ts_ms"] > k]
    recs = [json.loads(x) for f in sorted((tmp_path / "candidate" / "trend_sma").glob("*.jsonl"))
            for x in f.read_text(encoding="utf-8").splitlines()]
    got = [(r["data"]["symbol"], r["data"]["action"], r["ts_event_ms"]) for r in recs if r["type"] == "signal"]
    assert want and got == want                                           # the forward record IS the replay
    assert abs(fleet.books[0].account.equity() - ref_acct.equity()) < 1e-6
    assert not any(r["type"] == "shadow_fill" and r["ts_event_ms"] <= k for r in recs)   # priming never recorded


def _zip(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.name.replace(".zip", ".csv"), "\n".join(rows) + "\n")


def test_history_reads_monthly_then_daily_files_in_order(tmp_path):
    aug, sep = ms("2026-08-31T23:58:00"), ms("2026-09-01T00:00:00")
    _zip(tmp_path / "monthly" / "klines_1m" / "BTCUSDT" / "BTCUSDT-1m-2026-08.zip",
         [row(aug, 1, 1, 1, 1), row(aug + 60_000, 2, 2, 2, 2)])
    _zip(tmp_path / "klines_1m" / "BTCUSDT" / "BTCUSDT-1m-2026-09-01.zip",
         [row(sep, 3, 3, 3, 3), row(sep + 60_000, 4, 4, 4, 4), row(sep + 120_000, 5, 5, 5, 5)])
    got = [b.close for b in prime.history_bars(tmp_path, "BTCUSDT", 0, sep + 120_000)]
    assert got == [1.0, 2.0, 3.0, 4.0]                                  # ordered, stops at end_ms
