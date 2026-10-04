"""V2-5: Contract A producer, feature engine (Contract B), state builder (Contract C)."""
import ast
import math
import pathlib

import pytest

import trading.v2.features as F
from trading.replay.bars import INTERVAL_MS, Bar
from trading.v2.contracts import (DecisionState, FeatureSnapshot, MarketEvent, PositionState, RiskState,
                                  StrategyRef, UnusableSnapshot)
from trading.v2.events import iso_ms, kline_closed_event
from trading.v2.features import (Book, FeatureResult, compute_features, rsi_wilder, FEATURE_VERSION)
from trading.v2.state import build_decision_state

from test_v2_contracts import feature_snapshot

SYM = "BTCUSDT"
T0 = 1_791_000_000_000 - (1_791_000_000_000 % 3_600_000)   # an hour boundary


def bar(interval, i, close, vol=10.0, hi=None, lo=None, qv=None):
    step = INTERVAL_MS[interval]
    o = T0 + i * step
    hi = close * 1.001 if hi is None else hi
    lo = close * 0.999 if lo is None else lo
    return Bar(SYM, interval, o, o + step, close, hi, lo, close, vol,
               (close * vol) if qv is None else qv, 5, vol / 2)


class Store:
    def __init__(self, m1=(), h1=()):
        self.d = {("1m"): list(m1), ("1h"): list(h1)}

    def tail(self, symbol, interval, n):
        return tuple(self.d.get(interval, [])[-n:])


class Bk:
    def __init__(self, book):
        self.book = book

    def latest(self, symbol):
        return self.book


def lcg(seed):
    x = seed
    while True:
        x = (1103515245 * x + 12345) % (2 ** 31)
        yield x / 2 ** 31


def series(n, start=100000.0, drift=0.0001, noise=0.002, seed=7):
    g, c, out = lcg(seed), start, []
    for _ in range(n):
        c *= 1 + drift + (next(g) - 0.5) * noise
        out.append(c)
    return out


def make_store(n1=300, n1h=130, m1_seed=3, h1_seed=5):
    m1 = [bar("1m", i, c, vol=10 + (i * 7 % 13)) for i, c in enumerate(series(n1, seed=m1_seed))]
    last_end = m1[-1].end_ms
    # hourly bars that all end at or before the last 1m bar
    first_h = (last_end - n1h * 3_600_000 - T0) // 3_600_000
    h1 = [Bar(SYM, "1h", T0 + (first_h + i) * 3_600_000, T0 + (first_h + i + 1) * 3_600_000,
              c, c * 1.003, c * 0.997, c, 500.0, c * 500.0, 100, 250.0)
          for i, c in enumerate(series(n1h, seed=h1_seed, noise=0.01))]
    h1 = [b for b in h1 if b.end_ms <= last_end]
    return Store(m1, h1)


GOOD_BOOK_FOR = lambda store: Book(ts_ms=store.d["1m"][-1].end_ms, bid=100000.0, ask=100000.5, bid_qty=12.8, ask_qty=9.7)


def run(store=None, book="default", now_delta=120, **kw):
    store = store or make_store()
    if book == "default":
        book = GOOD_BOOK_FOR(store)
    now = store.d["1m"][-1].end_ms + now_delta
    return compute_features(SYM, store, Bk(book) if book is not None or kw.pop("nobook", False) else None, now, **kw), store, now


# ------------------------------------------------------------ RSI

def test_rsi_known_answer_from_wilders_textbook_example():
    closes = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
              45.8931, 46.0328, 45.6140, 46.2820, 46.2820]
    assert rsi_wilder(closes) == pytest.approx(70.53, abs=0.05)   # StockCharts RSI(14) first value
    assert rsi_wilder(closes + [46.0, 46.0328]) is not None


def reference_rsi(closes, n=14):
    ch = [b - a for a, b in zip(closes, closes[1:])]
    ag = sum(x for x in ch[:n] if x > 0) / n
    al = sum(-x for x in ch[:n] if x < 0) / n
    for x in ch[n:]:
        ag = (ag * (n - 1) + (x if x > 0 else 0)) / n
        al = (al * (n - 1) + (-x if x < 0 else 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def test_rsi_matches_an_independent_reference_on_random_walks():
    for seed in range(1, 8):
        c = series(80, seed=seed, drift=0.0, noise=0.02)
        assert rsi_wilder(c) == pytest.approx(reference_rsi(c), abs=1e-9)


def test_rsi_edges():
    assert rsi_wilder([float(i) for i in range(1, 40)]) == 100.0
    assert rsi_wilder([float(40 - i) for i in range(40)]) == pytest.approx(0.0, abs=1e-9)
    assert rsi_wilder([100.0] * 40) is None            # flat: undefined, NOT 50
    assert rsi_wilder([1.0] * 14) is None              # too short (needs 15)
    assert all(0 <= (rsi_wilder(series(60, seed=s, noise=0.05)) or 0) <= 100 for s in range(10))


# ------------------------------------------------------------ happy path

def test_snapshot_is_complete_valid_and_matches_hand_computation():
    res, store, now = run()
    assert res.ok and res.missing == ()
    s = res.snapshot
    assert isinstance(s, FeatureSnapshot) and s.feature_version == FEATURE_VERSION == "features-v1"
    m1 = store.d["1m"]
    c = [b.close for b in m1]
    assert s.returns["1m"] == pytest.approx(c[-1] / c[-2] - 1)
    assert s.returns["5m"] == pytest.approx(c[-1] / c[-6] - 1)
    assert s.returns["4h"] == pytest.approx(c[-1] / c[-241] - 1)
    w = m1[-60:]
    assert s.technical.distance_vwap == pytest.approx(c[-1] / (sum(b.quote_volume for b in w) / sum(b.volume for b in w)) - 1)
    base = [b.volume for b in m1[-61:-1]]
    mean = sum(base) / 60
    std = math.sqrt(sum((v - mean) ** 2 for v in base) / 59)
    assert s.volume.volume_zscore == pytest.approx((m1[-1].volume - mean) / std)
    assert s.volume.volume_1m == m1[-1].volume
    assert s.technical.rsi == pytest.approx(reference_rsi([b.close for b in store.d["1h"]][-F.N_1H:]))
    assert s.orderbook.imbalance == pytest.approx((12.8 - 9.7) / (12.8 + 9.7))
    assert (s.orderbook.bid_depth, s.orderbook.ask_depth) == (12.8, 9.7)
    assert res.spread_bps == pytest.approx(0.5 / 100000.25 * 1e4)
    assert s.as_of_utc == iso_ms(m1[-1].end_ms)
    assert s.data_quality.complete and s.data_quality.stale_ms == 120
    assert s.regime.liquidity == "normal" and s.regime.trend in ("bullish", "bearish", "neutral")
    assert set(s.returns) == {"1m", "5m", "15m", "1h", "4h"}
    assert FeatureSnapshot.from_json(s.to_json()) == s


def test_deterministic_and_pure_over_repeated_runs():
    store = make_store()
    first = run(store)[0].snapshot.to_json()
    assert all(run(store)[0].snapshot.to_json() == first for _ in range(200))


def test_snapshot_id_is_stable_per_symbol_time_and_definition():
    a, b = run()[0].snapshot, run()[0].snapshot
    assert a.snapshot_id == b.snapshot_id and a.snapshot_id.startswith("feat_")


# ------------------------------------------------------------ never synthetic: missing -> no snapshot

def assert_missing(res, fragment):
    assert not res.ok and res.snapshot is None
    assert any(fragment in m for m in res.missing), res.missing


def test_no_bars_at_all():
    res = compute_features(SYM, Store(), Bk(None), 0)
    assert_missing(res, "bars.1m")


def test_too_few_one_minute_bars_for_returns():
    s = make_store(n1=240)
    assert_missing(run(s)[0], "returns(need 241")


def test_too_few_hourly_bars_for_technicals():
    s = make_store(n1h=59)
    assert_missing(run(s)[0], "technical/regime(need 60")


def test_missing_book_is_missing_not_zero():
    res, *_ = run(book=None, nobook=True)
    assert_missing(res, "orderbook(none)")


def test_stale_future_and_invalid_books():
    s = make_store()
    end = s.d["1m"][-1].end_ms
    assert_missing(run(s, Book(end - 10_000, 100.0, 100.1, 1, 1))[0], "orderbook(stale")
    assert_missing(run(s, Book(end + 10_000, 100.0, 100.1, 1, 1))[0], "look-ahead")
    assert_missing(run(s, Book(end, 100.1, 100.0, 1, 1))[0], "orderbook(invalid)")     # crossed
    assert_missing(run(s, Book(end, 100.0, 100.1, 0, 0))[0], "orderbook(invalid)")     # no size
    assert_missing(run(s, Book(end, 0.0, 100.1, 1, 1))[0], "orderbook(invalid)")


def test_flat_hourly_prices_make_rsi_missing_not_fifty():
    s = make_store()
    s.d["1h"] = [Bar(SYM, "1h", b.open_ms, b.end_ms, 100.0, 100.0, 100.0, 100.0, 5, 500, 1, 2) for b in s.d["1h"]]
    res = run(s)[0]
    assert_missing(res, "rsi(undefined)")


def test_zero_volume_variance_is_missing_not_zero():
    s = make_store()
    s.d["1m"] = [Bar(SYM, "1m", b.open_ms, b.end_ms, b.open, b.high, b.low, b.close, 10.0, b.close * 10.0, 1, 5)
                 for b in s.d["1m"]]
    assert_missing(run(s)[0], "volume_zscore")


def test_multiple_gaps_are_all_reported():
    res = compute_features(SYM, make_store(n1=100, n1h=10), Bk(None), 10**13)
    joined = " | ".join(res.missing)
    assert "returns" in joined and "technical/regime" in joined and "orderbook" in joined
    assert not res.ok


# ------------------------------------------------------------ no look-ahead

def test_hourly_bar_ending_after_the_last_minute_bar_is_ignored():
    s = make_store()
    base = run(s)[0].snapshot
    last = s.d["1h"][-1]
    s.d["1h"].append(Bar(SYM, "1h", last.end_ms, last.end_ms + 3_600_000, 1.0, 9e9, 0.1, 1.0, 1, 1, 1, 1))
    s2 = run(s)[0].snapshot
    assert s2.technical == base.technical and s2.regime == base.regime


def test_unordered_bars_raise_rather_than_compute_garbage():
    s = make_store()
    s.d["1m"][-1], s.d["1m"][-2] = s.d["1m"][-2], s.d["1m"][-1]
    with pytest.raises(ValueError, match="strictly increasing"):
        run(s)


def test_bars_of_another_symbol_or_interval_are_not_used():
    s = make_store()
    alien = s.d["1m"][-1]
    s.d["1m"].append(Bar("ETHUSDT", "1m", alien.end_ms, alien.end_ms + 60000, 1, 1, 1, 1, 1, 1, 1, 1))
    assert run(s)[0].snapshot.as_of_utc == iso_ms(alien.end_ms)


# ------------------------------------------------------------ regime mapping reuses the existing classifier

@pytest.mark.parametrize("label,expected_vol", [("VOLATILE", "high"), ("RANGING", "normal"), ("TRENDING", "normal")])
def test_regime_mapping(monkeypatch, label, expected_vol):
    monkeypatch.setattr(F, "classify_regime", lambda rows: label)
    s = run()[0].snapshot
    assert s.regime.volatility == expected_vol
    if label != "TRENDING":
        assert s.regime.trend == "neutral"


def test_trending_label_maps_to_bullish_or_bearish_by_ema_order(monkeypatch):
    monkeypatch.setattr(F, "classify_regime", lambda rows: "TRENDING")
    up = make_store(h1_seed=5)
    up.d["1h"] = [Bar(SYM, "1h", b.open_ms, b.end_ms, 100 * 1.01 ** i, 100 * 1.01 ** i, 100 * 1.01 ** i,
                      100 * 1.01 ** i, 5, 500, 1, 2) for i, b in enumerate(up.d["1h"])]
    assert run(up)[0].snapshot.regime.trend == "bullish"
    dn = make_store()
    dn.d["1h"] = [Bar(SYM, "1h", b.open_ms, b.end_ms, 200 * 0.99 ** i, 200 * 0.99 ** i, 200 * 0.99 ** i,
                      200 * 0.99 ** i, 5, 500, 1, 2) for i, b in enumerate(dn.d["1h"])]
    assert run(dn)[0].snapshot.regime.trend == "bearish"


def test_real_classifier_calls_a_trend_on_a_clean_uptrend():
    s = make_store()
    s.d["1h"] = [Bar(SYM, "1h", b.open_ms, b.end_ms, 100 * 1.01 ** i, 100 * 1.0105 ** i, 100 * 1.0095 ** i,
                     100 * 1.01 ** i, 5, 500, 1, 2) for i, b in enumerate(s.d["1h"])]
    assert run(s)[0].snapshot.regime.trend == "bullish"


def test_liquidity_is_thin_above_the_spread_threshold():
    s = make_store()
    end = s.d["1m"][-1].end_ms
    thin = run(s, Book(end, 100000.0, 100060.0, 1, 1))[0].snapshot     # 6 bps
    ok = run(s, Book(end, 100000.0, 100040.0, 1, 1))[0].snapshot       # 4 bps
    assert thin.regime.liquidity == "thin" and ok.regime.liquidity == "normal"


# ------------------------------------------------------------ Contract A producer

def test_kline_closed_event_is_valid_stable_and_dedupable():
    b = bar("1m", 5, 100000.0)
    e = kline_closed_event(b, received_ms=b.end_ms + 120)
    assert isinstance(e, MarketEvent) and e.kind == "kline_closed" and e.timeframe == "1m"
    assert e.price == 100000.0 and e.event_time_utc == iso_ms(b.end_ms)
    assert e.feed_latency_ms == pytest.approx(120.0)
    assert e.event_id == kline_closed_event(b, received_ms=b.end_ms + 999).event_id   # same bar, same id
    assert e.event_id != kline_closed_event(bar("1m", 6, 100000.0), received_ms=0).event_id
    with pytest.raises(ValueError, match="unsupported interval"):
        kline_closed_event(Bar(SYM, "7m", 0, 1, 1, 1, 1, 1, 1, 1, 1, 1), 0)


def test_iso_ms_formats_milliseconds():
    assert iso_ms(1_791_083_882_007) == "2026-10-04T03:18:02.007Z"
    assert iso_ms(0) == "1970-01-01T00:00:00.000Z"


# ------------------------------------------------------------ Contract C builder

POS = PositionState(side="none", size=0, entry_price=None, unrealized_pnl=0)
RISK = RiskState(equity=10000, daily_pnl=-72, daily_loss_limit_remaining=428, current_exposure=0.0)
STRAT = StrategyRef(name="btc_breakout_v3", version="3.2.1")


def build(snapshot, **kw):
    args = dict(price=100000.0, spread_bps=0.5, position=POS, risk=RISK, strategy=STRAT, max_stale_ms=1000)
    args.update(kw)
    return build_decision_state(snapshot, **args)


def test_state_builder_produces_a_valid_state_referencing_the_snapshot():
    snap = run()[0].snapshot
    st = build(snap)
    assert isinstance(st, DecisionState) and st.features == snap.snapshot_id
    assert st.regime == snap.regime and st.symbol == SYM and st.market.price == 100000.0
    assert st.timestamp == run()[1].d["1m"][-1].end_ms // 1000
    assert build(snap).state_id == st.state_id and build(snap).state_hash() == st.state_hash()
    assert DecisionState.from_json(st.to_json()) == st


def test_state_builder_refuses_stale_and_incomplete_snapshots():
    snap = run(now_delta=5_000)[0].snapshot       # 5s stale
    with pytest.raises(UnusableSnapshot, match="stale"):
        build(snap, max_stale_ms=1000)
    assert build(snap, max_stale_ms=5000)
    d = feature_snapshot()
    d["data_quality"].update(complete=False, missing_fields=["orderbook.imbalance"])
    with pytest.raises(UnusableSnapshot, match="incomplete"):
        build(FeatureSnapshot.from_dict(d))


def test_state_changes_when_position_or_strategy_changes():
    snap = run()[0].snapshot
    a = build(snap)
    b = build(snap, position=PositionState(side="long", size=0.5, entry_price=100000.0, unrealized_pnl=1.0))
    c = build(snap, strategy=StrategyRef(name="btc_breakout_v3", version="3.2.2"))
    assert len({a.state_id, c.state_id}) == 2 and a.state_hash() != b.state_hash()


def test_modules_are_pure_no_clock_no_io():
    for mod in ("features", "state", "events"):
        src = pathlib.Path(f"src/trading/v2/{mod}.py").read_text()
        tree = ast.parse(src)
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                names.add((n.module or "").split(".")[0])
        assert names.isdisjoint({"time", "random", "socket", "requests", "os", "sqlite3", "subprocess", "logging"}), mod
        assert ".now(" not in src and "utcnow" not in src and "time.time" not in src, mod
