"""V2-16: multi-day context. The live numbers must be the studied numbers; no look-ahead; missing is missing."""
import math

import numpy as np
import pytest
from pydantic import ValidationError

from trading.replay.bars import Bar
from trading.v2.contracts import ContextFeatures, FeatureSnapshot
from trading.v2.daily_context import CONTEXT_FIELDS, N_CTX_1H, H, coin_row, cross_section, live_context
from trading.v2.features import snapshot_content_id, verify_snapshot

from test_v2_contracts import feature_snapshot, mutated

T0 = 1_700_000_000_000 - 1_700_000_000_000 % H


def series(sym, n=N_CTX_1H + 30, seed=1, drift=0.0002, start=100.0):
    rng = np.random.default_rng(seed)
    c = start * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    out = []
    for i in range(n):
        o = c[i - 1] if i else start
        out.append(Bar(sym, "1h", T0 + i * H, T0 + (i + 1) * H, o, max(o, c[i]) * 1.002, min(o, c[i]) * 0.998, c[i],
                       10.0 + i % 7, (10.0 + i % 7) * c[i], 5, 5.0))
    return out


UNI = {s: series(s, seed=k) for k, s in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"))}
AS_OF = UNI["BTCUSDT"][-1].end_ms


def arrays(bars):
    a = np.array([(b.open_ms, b.open, b.high, b.low, b.close, b.quote_volume) for b in bars])
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]


def test_live_equals_the_study_path():
    ctx, missing = live_context(UNI, "ETHUSDT", AS_OF)
    assert ctx is not None and missing == ()
    rows = {}
    for s, b in UNI.items():
        t, o, h, l, c, qv = arrays(b)
        rows[s] = coin_row(t, o, h, l, c, qv, len(t) - 1)
    t, o, h, l, c, qv = arrays(UNI["BTCUSDT"])
    above = 1.0 if c[-1] > c[-1200:].mean() else 0.0
    full = {**rows["ETHUSDT"], **cross_section(rows, "ETHUSDT", rows["BTCUSDT"], above)}
    assert [ctx[f] for f in CONTEXT_FIELDS] == pytest.approx([full[f] for f in CONTEXT_FIELDS], rel=1e-12)
    ContextFeatures.from_dict(ctx)


def test_no_look_ahead():
    early = UNI["BTCUSDT"][-10].end_ms
    a, _ = live_context(UNI, "SOLUSDT", early)
    cut = {s: [b for b in bars if b.end_ms <= early] for s, bars in UNI.items()}
    b, _ = live_context(cut, "SOLUSDT", early)
    assert a == b


def test_gap_short_stale_and_missing_btc_are_missing_not_guessed():
    gap = {**UNI, "SOLUSDT": UNI["SOLUSDT"][:500] + UNI["SOLUSDT"][501:]}
    assert live_context(gap, "SOLUSDT", AS_OF)[0] is None
    short = {**UNI, "SOLUSDT": UNI["SOLUSDT"][-100:]}
    ctx, miss = live_context(short, "SOLUSDT", AS_OF)
    assert ctx is None and any("need 1250" in m for m in miss)
    assert live_context(UNI, "SOLUSDT", AS_OF + 3 * H)[0] is None                       # last closed hour is too old
    no_btc = {k: v for k, v in UNI.items() if k != "BTCUSDT"}
    assert live_context(no_btc, "SOLUSDT", AS_OF)[0] is None


def test_ranks_are_in_unit_interval_and_btc_regime_is_binary():
    for s in UNI:
        c, _ = live_context(UNI, s, AS_OF)
        assert 0 <= c["xs_rank7"] <= 1 and 0 <= c["xs_rank30"] <= 1 and c["btc_above_sma50"] in (0.0, 1.0)
        assert all(math.isfinite(c[f]) for f in CONTEXT_FIELDS) and c["n_universe"] == 4


# ------------------------------------------------------------ contract and ids

def ctx_dict():
    return live_context(UNI, "BTCUSDT", AS_OF)[0]


def test_context_requires_features_v2():
    with pytest.raises(ValidationError, match="features-v2"):
        FeatureSnapshot.from_dict(mutated(feature_snapshot, context=ctx_dict()))
    FeatureSnapshot.from_dict(mutated(feature_snapshot, feature_version="features-v2", context=ctx_dict()))


def test_old_snapshot_ids_are_unchanged_by_the_new_optional_field():
    d = feature_snapshot()
    d.pop("context", None)
    old_id = snapshot_content_id(d)
    assert snapshot_content_id({**d, "context": None}) == old_id
    s = FeatureSnapshot.from_dict({**d, "snapshot_id": old_id})
    assert verify_snapshot(s)


def test_context_changes_the_id():
    d = mutated(feature_snapshot, feature_version="features-v2", context=ctx_dict())
    assert snapshot_content_id(d) != snapshot_content_id(mutated(feature_snapshot, feature_version="features-v2"))


# ------------------------------------------------------------ pipeline + runner

def test_pipeline_sends_context_to_jev_and_logs_missing(tmp_path):
    from test_v2_shadow import build, make_bars
    p = build(tmp_path)
    p.context_source = lambda sym, t: (ctx_dict(), ())
    recs = [r for r in p.run(make_bars()) if r["outcome"] != "no_snapshot"]
    assert recs and all(r["features"]["feature_version"] == "features-v2" for r in recs)
    assert all(r["features"]["context"]["context_version"] == "context-v1" for r in recs)
    p2 = build(tmp_path / "b")
    p2.context_source = lambda sym, t: (None, ("context.ETHUSDT(stale)",))
    recs2 = [r for r in p2.run(make_bars()) if r["outcome"] != "no_snapshot"]
    assert recs2 and all(r["features"]["feature_version"] == "features-v1" and r["features"]["context"] is None
                         for r in recs2)
    assert all(r["context_missing"] == ["context.ETHUSDT(stale)"] for r in recs2)
    assert p2.stats["context:missing"] == len(recs2)


def test_a_context_source_that_crashes_never_breaks_the_cycle(tmp_path):
    from test_v2_shadow import build, make_bars
    p = build(tmp_path)
    def boom(sym, t):
        raise RuntimeError("x")
    p.context_source = boom
    recs = [r for r in p.run(make_bars()) if r["outcome"] != "no_snapshot"]
    assert recs and p.stats["cycle_error"] == 0 and recs[0]["context_missing"] == ["context(error RuntimeError)"]


def test_runner_primes_and_uses_the_context_universe(tmp_path):
    from trading.v2.runner import Runner, RunnerConfig, demo_config
    from test_v2_runner import BASE, M, Client, Clock, FakeFeed, WF2, SYM, LONG_RAW
    clock = Clock(BASE + 120 * M)
    syms = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    feed = FakeFeed(clock, symbols=syms, n1h=1400)
    c = RunnerConfig(**{**demo_config(("BTCUSDT",), 15, tmp_path).__dict__, "context_symbols": syms})
    r = Runner(c, feed, WF2, jev_client=Client(LONG_RAW), clock=clock.time, sleep=clock.sleep)
    r.run(max_steps=75)                                   # long enough to cross an hour: the hourly refresh must run
    recs = r.pipeline.stats
    assert recs["context:ok"] >= 1
    assert all(len(r.ctx_bars[s]) >= N_CTX_1H for s in syms)
    import json
    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    assert hb["context"]["ok"] >= 1 and hb["context"]["refresh_ms"] is not None
