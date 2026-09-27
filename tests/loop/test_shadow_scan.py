"""P4c live scan: provisional, read-only, written once a minute, never part of the chain."""
from __future__ import annotations

import copy
import heapq
import json

import numpy as np
import pytest
from test_replay import daily_minutes, ms

from trading.replay.broker import CostModel
from trading.scanner.bars import ticks_from_bar
from trading.shadow import prime, scan
from trading.shadow.chain import verify
from trading.shadow.fleet import ShadowFleet

D0 = ms("2021-01-04T00:00:00")
DAY = 86_400_000


def bars(n=420, seed=2):
    rng = np.random.default_rng(seed)
    out = {}
    for i, s in enumerate(("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "SOLUSDT", "DOGEUSDT")):
        c = 100 * np.cumprod(1 + 0.001 * (i - 2) + 0.02 * rng.standard_normal(n))
        out[s] = daily_minutes(s, D0, c.tolist(), per_day=3)
    return out


def primed_fleet(tmp_path, names, k_days=400):
    B = bars()
    k = D0 + k_days * DAY
    fleet = ShadowFleet(sorted(B), [{"lane": "rejected", "strategy": n, "slots": 3} for n in names], tmp_path,
                        forward_start_ms=k, costs=CostModel(0, 0, 0), grace_ms=None)
    prime.prime_history(fleet, lambda s: [b for b in B[s] if b.end_ms <= k])
    live = [ticks_from_bar(b) for s in B for b in B[s] if b.open_ms >= k]
    return fleet, list(heapq.merge(*live, key=lambda t: (t.ts_exchange_ms, t.symbol)))


def test_scanning_never_changes_state_or_the_recorded_signals(tmp_path):
    # two identical fleets over the same ticks: one scans every minute, one never scans
    names = ["donchian_ensemble", "tsmom_risk_managed", "xs_momentum"]
    a, ticks = primed_fleet(tmp_path / "a", names)
    b, _ = primed_fleet(tmp_path / "b", names)
    b._maybe_scan = lambda t_ms: None
    for t in ticks:
        a.on_tick(t)
        b.on_tick(t)
    assert list((tmp_path / "a" / "scan").glob("*.json")) and not (tmp_path / "b" / "scan").exists()
    for x, y in zip(a.books, b.books):
        assert x.scanner.core.strategies[0].__dict__ == y.scanner.core.strategies[0].__dict__, x.name
        sig = lambda root: [json.loads(l)["data"] for f in sorted((root / "rejected" / x.name).glob("*.jsonl"))
                            for l in f.read_text(encoding="utf-8").splitlines() if '"type": "signal"' in l
                            or '"type":"signal"' in l]
        assert sig(tmp_path / "a") == sig(tmp_path / "b"), x.name


def test_scans_are_written_each_minute_and_never_enter_the_chain(tmp_path):
    fleet, ticks = primed_fleet(tmp_path, ["donchian_ensemble", "tsmom_risk_managed"])
    for t in ticks:
        fleet.on_tick(t)
    files = sorted(p.name for p in (tmp_path / "scan").glob("*.json"))
    assert files == ["rejected__donchian_ensemble.json", "rejected__tsmom_risk_managed.json"]
    doc = json.loads((tmp_path / "scan" / "rejected__tsmom_risk_managed.json").read_text(encoding="utf-8"))
    assert doc["label"].startswith("PROVISIONAL") and doc["next_decision_ms"] % DAY == 0
    for lane_dir in (tmp_path / "rejected").iterdir():
        recs = [json.loads(x) for f in lane_dir.glob("*.jsonl") for x in f.read_text(encoding="utf-8").splitlines()]
        assert verify(lane_dir)["ok"] and not any("provisional" in json.dumps(r) for r in recs)


def test_donchian_scan_reports_the_distance_to_the_next_breakout():
    class Ctx:
        def __init__(self, closes, price):
            self.c, self.p = closes, price

        def history(self, sym, iv, n):
            class B:
                def __init__(self, c, e=0):
                    self.close, self.end_ms = c, e
            return [B(self.p, 1)] if iv == "1m" else [B(x) for x in self.c[-n:]]

    from trading.replay.plugins.donchian import DonchianEnsemble
    s = DonchianEnsemble(lookbacks=(5,))
    s._seen = ["X"]
    rows = scan.scan_donchian(s, Ctx([100, 104, 110, 103, 101, 102], 105.0))
    assert rows[0]["state"] == "0/1 lookbacks open"
    assert rows[0]["detail"] == "next lookback opens at +4.76%"             # 5-day high 110 vs price 105
    assert s._stop == {}                                                     # nothing written


def test_next_decision_times():
    t = ms("2021-01-06T13:00:00")                                            # a Wednesday
    assert scan.next_daily(t) == ms("2021-01-07T00:00:00")
    assert scan.next_weekly(t) == ms("2021-01-11T00:00:00")                  # next Monday
