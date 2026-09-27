"""A6 cross-sectional momentum: weekly top quintile by 3-week return."""
from __future__ import annotations

import numpy as np
from test_replay import daily_minutes, ms

from trading.replay.broker import CostModel, PaperAccount
from trading.replay.engine import replay
from trading.scanner.registry import DEFAULT


def test_holds_the_top_quintile_by_three_week_return():
    d0 = ms("2021-01-04T00:00:00")                                   # a Monday
    syms = [f"C{i}USDT" for i in range(9)] + ["BTCUSDT"]
    streams = {}
    for i, s in enumerate(syms):
        c = 100 * np.cumprod(np.full(60, 1 + 0.002 * i))              # steady, ranked by i
        streams[s] = daily_minutes(s, d0, c.tolist(), per_day=2)
    strat = DEFAULT.build("xs_momentum")
    acct = PaperAccount(1000, 2, CostModel(0, 0, 0))                  # 10 coins // 5 = 2
    replay(streams, [strat], {"net": acct})
    assert set(acct.positions) == {"BTCUSDT", "C8USDT"}               # the two highest growth rates


def test_ranks_on_exactly_three_weeks():
    d0 = ms("2021-01-04T00:00:00")
    flat = [100.0] * 40
    jump_old = [100.0] * 5 + [200.0] * 35                             # jumped 5 weeks before the last Monday
    jump_new = [100.0] * 25 + [110.0] * 15                            # +10% inside the last 3 weeks
    streams = {"BTCUSDT": daily_minutes("BTCUSDT", d0, flat, per_day=2)}
    streams.update({f"F{i}USDT": daily_minutes(f"F{i}USDT", d0, flat, per_day=2) for i in range(2)})
    streams["OLDUSDT"] = daily_minutes("OLDUSDT", d0, jump_old, per_day=2)
    streams["NEWUSDT"] = daily_minutes("NEWUSDT", d0, jump_new, per_day=2)
    strat = DEFAULT.build("xs_momentum")
    replay(streams, [strat], {"net": PaperAccount(1000, 1, CostModel(0, 0, 0))})
    assert max(strat.last_scores, key=strat.last_scores.get) == "NEWUSDT"
    assert strat.last_scores["OLDUSDT"] == 0.0


def test_registered_in_the_cross_section_family():
    from trading.replay.gate import family_of
    assert DEFAULT.get("xs_momentum").family == family_of("xs_momentum") == "cross_section"
