"""Stances from the repo's existing strategies on daily bars. Synthetic data, no network."""
import math

import pytest

from trading.replay.bars import Bar
from trading.v2.contracts import DecisionState, FALLBACK_STRATEGIES
from trading.v2.fallback import GatedSource, aggregate
from trading.v2.stances import (DONCHIAN_MIN_BARS, STALE_AFTER_MS, ReplayStanceSource, donchian_stances,
                                donchian_targets, trend_stances)

from test_v2_contracts import decision_state

DAY = 86_400_000
D0 = 1_700_006_400_000          # a UTC midnight


def bars(sym, closes, qv=5_000_000.0, t0=D0, end_offset=0):
    out = []
    for i, c in enumerate(closes):
        o = t0 + i * DAY
        out.append(Bar(sym, "1d", o, o + DAY + end_offset, c, c * 1.01, c * 0.99, c, qv / c, qv, 100, qv / c / 2))
    return out


def walk(n, drift, start=100.0, noise=0.01, seed=1):
    x, c, out = seed, start, []
    for _ in range(n):
        x = (1103515245 * x + 12345) % 2 ** 31
        c *= 1 + drift + (x / 2 ** 31 - 0.5) * noise
        out.append(c)
    return out


def market(drift_btc, drift_alt, n=500, **kw):
    return {"BTCUSDT": bars("BTCUSDT", walk(n, drift_btc, seed=1), **kw),
            "ETHUSDT": bars("ETHUSDT", walk(n, drift_alt, seed=2), **kw),
            "SOLUSDT": bars("SOLUSDT", walk(n, drift_alt, seed=3), **kw)}


# ------------------------------------------------------------ donchian (the real plugin)

def test_donchian_is_long_in_a_sustained_uptrend_and_flat_in_a_downtrend():
    up = donchian_stances(market(0.004, 0.004))
    assert set(up.values()) == {"LONG"}, up
    down = donchian_stances(market(-0.004, -0.004))
    assert set(down.values()) == {"FLAT"}, down


def test_donchian_cannot_judge_short_history_or_a_missing_btc_or_a_stale_symbol():
    short = donchian_stances(market(0.004, 0.004, n=DONCHIAN_MIN_BARS - 1))
    assert set(short.values()) == {"UNAVAILABLE"}
    m = market(0.004, 0.004)
    del m["BTCUSDT"]
    assert set(donchian_stances(m).values()) == {"UNAVAILABLE"}
    m = market(0.004, 0.004)
    m["SOLUSDT"] = m["SOLUSDT"][:-3]                                  # its newest bar is 3 days old
    got = donchian_stances(m)
    assert got["SOLUSDT"] == "UNAVAILABLE" and got["BTCUSDT"] == "LONG"
    hourly = {"BTCUSDT": [Bar("BTCUSDT", "1h", 0, 3600000, 1, 1, 1, 1, 1, 1, 1, 1)]}
    assert donchian_stances(hourly) == {"BTCUSDT": "UNAVAILABLE"}


def test_a_zero_weight_exit_signal_on_a_held_coin_is_flat_not_long():
    """The plugin emits weight 0.0 on the day it exits a coin it holds. 'Any signal' is not 'long'."""
    up = walk(450, 0.004, seed=1)
    seen_zero = seen_long = 0
    for k in range(1, 80):
        crash = list(up)
        c = crash[-1]
        for _ in range(k):
            c *= 0.93
            crash.append(c)
        m = {"BTCUSDT": bars("BTCUSDT", crash), "ETHUSDT": bars("ETHUSDT", walk(450 + k, 0.004, seed=2))}
        w = donchian_targets(m)["BTCUSDT"]
        st = donchian_stances(m)["BTCUSDT"]
        if w == 0.0:
            seen_zero += 1
            assert st == "FLAT", (k, w, st)                          # the exit day
        if w is not None and w > 0:
            seen_long += 1
            assert st == "LONG", (k, w, st)
        if w is None:
            assert st == "FLAT"
    assert seen_zero >= 1 and seen_long >= 1, (seen_zero, seen_long)  # the sweep really visits both situations


def test_donchian_ignores_illiquid_symbols_as_the_plugin_does():
    m = market(0.004, 0.004)
    m["SOLUSDT"] = bars("SOLUSDT", walk(500, 0.004, seed=3), qv=100_000.0)       # far below $2M/day
    got = donchian_stances(m)
    assert got["SOLUSDT"] == "FLAT" and got["BTCUSDT"] == "LONG"


def test_donchian_is_deterministic_and_does_not_mutate_its_input():
    m = market(0.004, 0.004)
    before = {s: list(v) for s, v in m.items()}
    a, b = donchian_stances(m), donchian_stances(m)
    assert a == b and m == before


# ------------------------------------------------------------ trend_follower's documented rule (reconstruction)

def btc_up(n=500):
    return bars("BTCUSDT", walk(n, 0.003, seed=1))


def test_trend_long_when_the_coin_is_above_sma50_and_btc_above_sma200():
    got = trend_stances({"BTCUSDT": btc_up(), "ETHUSDT": bars("ETHUSDT", walk(500, 0.004, seed=2))})
    assert got == {"BTCUSDT": "LONG", "ETHUSDT": "LONG"}


def test_trend_is_flat_when_the_coin_is_below_its_sma50():
    got = trend_stances({"BTCUSDT": btc_up(), "ETHUSDT": bars("ETHUSDT", walk(500, -0.004, seed=2))})
    assert got["ETHUSDT"] == "FLAT" and got["BTCUSDT"] == "LONG"


def test_trend_never_enters_while_btc_is_below_its_sma200():
    down_btc = bars("BTCUSDT", walk(500, -0.003, seed=1))
    got = trend_stances({"BTCUSDT": down_btc, "ETHUSDT": bars("ETHUSDT", walk(500, 0.004, seed=2))})
    assert got["ETHUSDT"] == "FLAT"


def test_the_btc_filter_is_not_an_exit_a_held_coin_stays_held():
    # BTC rises for 300 days, then falls below its SMA200; the coin keeps rising throughout: entered early, never exited
    btc = walk(300, 0.004, seed=1)
    c = btc[-1]
    for _ in range(300):
        c *= 0.99
        btc.append(c)
    coin = walk(600, 0.003, seed=2)
    got = trend_stances({"BTCUSDT": bars("BTCUSDT", btc), "ETHUSDT": bars("ETHUSDT", coin)})
    assert btc[-1] < sum(btc[-200:]) / 200                                  # BTC really is below its SMA200 now
    assert got["ETHUSDT"] == "LONG"


def test_trend_hard_stop_exits_even_while_the_coin_is_still_above_sma50():
    closes = [80.0] * 400 + [100.0] * 5 + [84.0]                           # entry ~100, then -16% but SMA50 ~82
    sma50 = sum(closes[-50:]) / 50
    assert closes[-1] > sma50 and closes[-1] / 100.0 - 1 <= -0.15          # above trend, past the hard stop
    btc = bars("BTCUSDT", walk(len(closes), 0.003, seed=1))
    got = trend_stances({"BTCUSDT": btc, "ETHUSDT": bars("ETHUSDT", closes)})
    assert got["ETHUSDT"] == "FLAT"


def test_trend_cannot_judge_short_history():
    got = trend_stances({"BTCUSDT": btc_up(), "NEWUSDT": bars("NEWUSDT", walk(59, 0.01, seed=5))})
    assert got["NEWUSDT"] == "UNAVAILABLE"
    short_btc = trend_stances({"BTCUSDT": btc_up(204), "ETHUSDT": bars("ETHUSDT", walk(500, 0.004, seed=2))})
    assert set(short_btc.values()) == {"UNAVAILABLE"}
    assert set(trend_stances({"ETHUSDT": bars("ETHUSDT", walk(500, 0.004))}).values()) == {"UNAVAILABLE"}


# ------------------------------------------------------------ the FallbackSource

def state(symbol="BTCUSDT", ts=None):
    d = decision_state()
    d["symbol"] = symbol
    if ts is not None:
        d["timestamp"] = ts
    return DecisionState.from_dict(d)


def fresh_source():
    s = ReplayStanceSource()
    m = market(0.004, 0.004)
    now = m["BTCUSDT"][-1].end_ms
    s.refresh(m, now)
    return s, now


def test_source_reports_the_four_named_strategies_and_never_the_failed_ones():
    s, now = fresh_source()
    got = dict(s.stances(state(ts=now // 1000)))
    assert set(got) == set(FALLBACK_STRATEGIES)
    assert got["trend_follower"] == "LONG" and got["donchian_ensemble"] == "LONG"
    assert got["tsmom"] == got["ctrend"] == "UNAVAILABLE"                 # failed the repo's own gate
    assert aggregate(got, "none").action == "LONG" and aggregate(got, "none").confidence == 0.5


def test_source_is_unavailable_when_never_refreshed_stale_or_unknown_symbol():
    assert set(ReplayStanceSource().stances(state()).values()) == {"UNAVAILABLE"}
    s, now = fresh_source()
    assert set(s.stances(state(ts=(now + STALE_AFTER_MS + 1000) // 1000)).values()) == {"UNAVAILABLE"}
    assert s.stances(state("DOGEUSDT", ts=now // 1000))["trend_follower"] == "UNAVAILABLE"


def test_gated_source_composes_with_it():
    s, now = fresh_source()
    g = GatedSource(s)
    assert dict(g.stances(state(ts=now // 1000)))["donchian_ensemble"] == "LONG"


@pytest.mark.parametrize("trend,position,expected", [
    ("LONG", "none", "LONG"), ("LONG", "long", "HOLD"), ("LONG", "short", "EXIT"),
    ("FLAT", "none", "NO_TRADE"), ("FLAT", "long", "EXIT"), ("FLAT", "short", "EXIT"),
])
def test_legacy_action_maps_the_current_rules_stance_through_the_position(trend, position, expected):
    s, now = fresh_source()
    s.trend["BTCUSDT"] = trend
    assert s.legacy_action("BTCUSDT", position, now) == expected


def test_legacy_action_is_none_when_it_cannot_judge():
    s, now = fresh_source()
    assert s.legacy_action("DOGEUSDT", "none", now) is None
    assert s.legacy_action("BTCUSDT", "none", now + STALE_AFTER_MS + 1) is None
    s.trend["BTCUSDT"] = "UNAVAILABLE"
    assert s.legacy_action("BTCUSDT", "none", now) is None
    assert ReplayStanceSource().legacy_action("BTCUSDT", "none", 0) is None
