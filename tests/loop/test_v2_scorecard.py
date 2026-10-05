"""V2-16: the scorecard grades decisions against what happened next, never guesses a pending one."""
import pytest

from trading.replay.bars import Bar
from trading.v2.scorecard import Path1h, score, main

H = 3_600_000
T = 1_760_000_000_000 - 1_760_000_000_000 % H


def bars(prices, sym="BTCUSDT", t0=T, hi=0.0, lo=0.0):
    out = []
    for i, p in enumerate(prices):
        o = prices[i - 1] if i else p
        out.append(Bar(sym, "1h", t0 + i * H, t0 + (i + 1) * H, o, max(o, p) * (1 + hi), min(o, p) * (1 - lo), p,
                       1.0, p, 1, 0.5))
    return out


def rec(t, price, action="LONG", p_long=0.7, edge=0.6, src="jev", dual=None, sym="BTCUSDT", advice=None):
    dec = {"source": "jev" if src == "jev" else "fallback", "action": action, "confidence": 0.8,
           "fallback_strategy": None if src == "jev" else "system_one_gateway",
           "probabilities": {"LONG": p_long}, "signals": {"expected_edge": edge}}
    r = {"type": "v2_decision_cycle", "symbol": sym, "ts_event_ms": t, "state": {"market": {"price": price}},
         "decision": None if advice else dec, "gateway_dual_run": None}
    if dual:
        r["gateway_dual_run"] = {"decision": {**dec, "source": "fallback", "action": dual,
                                              "fallback_strategy": "system_one_gateway"}}
    if advice:
        r["advice"] = advice
    return r


UP = [100 + i * 0.5 for i in range(400)]                       # +7% reached after ~14h, never -3.5%
DOWN = [100 - i * 0.3 for i in range(400)]


def test_forward_returns_and_barrier():
    p = Path1h(bars(UP))
    res = score([rec(T, 100.0)], {"BTCUSDT": p})
    s = res["sources"]["jev"]
    assert s["graded_24h"] == 1 and s["directional_hit_rate_24h"] == 1.0
    assert s["barrier_base_rate"] == 1.0 and s["p_long"]["brier"] == pytest.approx(0.09)


def test_pending_is_not_graded():
    p = Path1h(bars(UP[:3]))
    res = score([rec(T + 2 * H, 101.0)], {"BTCUSDT": p})
    s = res["sources"]["jev"]
    assert s["graded_24h"] == 0 and s["graded_barrier"] == 0 and res["pending"] == 1


def test_stop_first_in_a_shared_bar():
    p = Path1h(bars([100.0] * 400, hi=0.08, lo=0.04))
    assert p.barrier(T, 100.0) == 0.0


def test_dual_run_and_agreement():
    p = Path1h(bars(DOWN))
    res = score([rec(T, 100.0, action="NO_TRADE", dual="NO_TRADE"), rec(T + H, 99.7, action="LONG", dual="HOLD")],
                {"BTCUSDT": p})
    assert res["agreement_jev_x_gateway"] == {"INACTIVE|INACTIVE": 1, "LONG|INACTIVE": 1}
    assert res["sources"]["jev"]["directional_hit_rate_24h"] == 0.0
    assert res["sources"]["gateway_dual"]["decisions"] == 2


def test_gateway_primary_is_its_own_source():
    res = score([rec(T, 100.0, src="gateway", action="NO_TRADE")], {"BTCUSDT": Path1h(bars(UP))})
    assert "gateway" in res["sources"] and "jev" not in res["sources"]


def test_position_advice_exit_vs_hold():
    adv = {"source": "jev", "action": "EXIT",
           "position": {"side": "LONG", "entry": 95.0, "stop": 92.0, "target": 120.0}}
    p = Path1h(bars(DOWN))                     # price falls through the stop: exiting at 100 was better
    res = score([rec(T, 100.0, advice=adv)], {"BTCUSDT": p})
    a = res["position_advice"]["jev"]
    assert a["exits"] == 1 and a["exit_precision"] == 1.0 and a["mean_exit_minus_hold"] > 0
    p2 = Path1h(bars(UP))                      # price runs to the target: holding was better
    a2 = score([rec(T, 100.0, advice={**adv, "action": "HOLD"})], {"BTCUSDT": p2})["position_advice"]["jev"]
    assert a2["hold_precision"] == 1.0


def test_cli_reads_a_real_chain(tmp_path):
    from trading.shadow.chain import ShadowChain
    ch = ShadowChain(tmp_path, "v2cycle", "pipeline", T)
    ch.write({**rec(T, 100.0), "ts_event_ms": T})
    assert main(["--out", str(tmp_path)], fetch=lambda s, n, now: bars(UP)) == 0
    import json
    out = json.loads((tmp_path / "scorecard.json").read_text())
    assert out["sources"]["jev"]["decisions"] == 1


def test_e13_review_flag_after_twenty_bad_exits():
    adv = {"source": "jev", "action": "EXIT", "position": {"side": "LONG", "entry": 95.0, "stop": 90.0, "target": 120.0}}
    p = Path1h(bars(UP))                                    # holding to the target always beat exiting
    recs = [rec(T + k * H, 100.0 + k * 0.5, advice=adv) for k in range(20)]
    a = score(recs, {"BTCUSDT": p})["position_advice"]["jev"]
    assert a["exits"] == 20 and a["exit_precision"] == 0.0 and a["e13_review"] is True
    a19 = score(recs[:19], {"BTCUSDT": p})["position_advice"]["jev"]
    assert a19["e13_review"] is False
