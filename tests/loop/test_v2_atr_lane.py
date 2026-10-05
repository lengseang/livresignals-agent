"""V2-17 / owner decision E16: paper lane B with a volatility-scaled bracket."""
import pytest

from trading.v2.contracts import FeatureSnapshot
from trading.v2.policy import ExitParams, resolve_exit_params

from test_v2_contracts import feature_snapshot, mutated
from test_v2_daily_context import ctx_dict

ATR = ExitParams(stop_pct=0.035, target_pct=0.07, atr_mult=1.5)


def snap(atr_d=None):
    if atr_d is None:
        return FeatureSnapshot.from_dict(mutated(feature_snapshot, feature_version="features-v1"))
    c = {**ctx_dict(), "atr_d": atr_d}
    return FeatureSnapshot.from_dict(mutated(feature_snapshot, feature_version="features-v2", context=c))


def test_fixed_mode_is_unchanged():
    fx = ExitParams(0.035, 0.07)
    assert resolve_exit_params(fx, snap(0.02)) is fx and fx.mode == "fixed" and ATR.mode == "atr"


@pytest.mark.parametrize("atr_d,stop", [(0.02, 0.03), (0.002, 0.01), (0.08, 0.05)])
def test_atr_mode_scales_and_clamps(atr_d, stop):
    ep = resolve_exit_params(ATR, snap(atr_d))
    assert ep.stop_pct == pytest.approx(stop) and ep.target_pct == pytest.approx(2 * stop) and ep.mode == "fixed"


def test_atr_mode_without_context_proposes_nothing():
    assert resolve_exit_params(ATR, snap()) is None


def test_invalid_atr_params_rejected():
    with pytest.raises(ValueError):
        ExitParams(0.035, 0.07, atr_mult=1.5, min_stop=0.06, max_stop=0.05)


def test_pipeline_lane_b_trades_the_scaled_bracket(tmp_path):
    from test_v2_shadow import build, make_bars, Ctx
    c = ctx_dict()
    p = build(tmp_path)
    p.context_source = lambda s, t: (c, ())
    base = p.provider.context

    class C(Ctx):
        def context(self, symbol, as_of_ms):
            from dataclasses import replace
            return replace(base(symbol, as_of_ms), exit_params=ATR)
    p.provider = C()
    recs = [r for r in p.run(make_bars()) if r.get("proposal")]
    assert recs
    want = min(0.05, max(0.01, 1.5 * c["atr_d"]))
    r = recs[0]
    assert r["proposal"]["stop_pct"] == pytest.approx(want, abs=1e-6)
    assert r["exit_params"] == {"mode": "atr", "stop_pct": pytest.approx(want, abs=1e-6),
                                "target_pct": pytest.approx(2 * want, abs=1e-6)}


def test_approved_lane_b_config_and_cli(tmp_path, monkeypatch):
    from trading.v2.approved import ATR_EXIT, approved_config
    assert approved_config(("BTCUSDT",), 15, tmp_path, exit_mode="atr").exit_params is ATR_EXIT
    assert approved_config(("BTCUSDT",), 15, tmp_path).exit_params.mode == "fixed"
    with pytest.raises(ValueError):
        approved_config(("BTCUSDT",), 15, tmp_path, exit_mode="wide")
    import trading.v2.runner as R
    seen = {}

    class FakeRunner:
        def __init__(self, cfg, feed, wf, **kw):
            seen["cfg"] = cfg

        def run(self, **kw):
            (seen["cfg"].out_dir / "heartbeat.json").write_text("{}")
    monkeypatch.setattr(R, "Runner", FakeRunner)
    monkeypatch.setattr(R, "BinancePublic", lambda: object())
    assert R.main(["runner", "--approved", "--exit-mode", "atr", "--out", str(tmp_path)]) == 0
    assert seen["cfg"].exit_params.mode == "atr"
