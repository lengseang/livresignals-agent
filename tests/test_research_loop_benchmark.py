"""The research loop must not announce a "NEW BEST" that fails HARD RULE 3.

Regression test for the defect found on 2026-10-12:

  * `research_loop.py` computed `bench_btc_median` / `bench_eql_median` and
    PRINTED them in the findings header, but never used them — `score()`
    ranked configs against each other only (pos_folds, median).
  * So a config that is positive in every fold yet returns LESS than BTC
    buy&hold would still be crowned "NEW BEST" and pushed to Telegram.
  * The system's own HARD RULE 3 (SYSTEM_MAP.md §4) says: "ALWAYS compare
    to BTC buy&hold. If you don't beat it, you have no edge."

These tests pin the benchmark admissibility gate.

Mutation check: delete the `admissible(...)` gate in main() (revert the
`if ok_bench:` branch) and `test_new_best_suppressed_when_lagging_benchmark`
fails, because the suppressed claim reappears in `changed`.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "research_loop", ROOT / "scripts" / "research_loop.py")
rl = importlib.util.module_from_spec(_spec)
sys.modules["research_loop"] = rl
_spec.loader.exec_module(rl)


def _row(pos_folds, median):
    return {"pos_folds": pos_folds, "median": median, "n_folds": 4,
            "cfg": {"topn": 3}, "trades": 700,
            "rets": [median] * 4}


def test_admissible_true_when_beating_benchmark():
    ok, detail = rl.admissible(_row(4, 19.78), bench_btc_median=-21.45)
    assert ok is True
    assert "BEATS" in detail and "+41.23pp" in detail


def test_admissible_false_when_lagging_benchmark():
    # 4/4 positive folds but LOWER median than BTC buy&hold.
    ok, detail = rl.admissible(_row(4, 10.0), bench_btc_median=25.0)
    assert ok is False
    assert "LAGS" in detail and "-15.00pp" in detail


def test_admissible_faileth_safe_on_missing_benchmark():
    ok, _ = rl.admissible(_row(4, 10.0), bench_btc_median=None)
    assert ok is False, "a missing benchmark must BLOCK the claim, not pass it"
    ok2, _ = rl.admissible({"pos_folds": 4}, bench_btc_median=1.0)
    assert ok2 is False


def test_score_still_prefers_folds_then_median():
    # Guard against accidentally reordering the ranking key.
    assert rl.score(_row(4, 5)) > rl.score(_row(3, 99))


def test_new_best_suppressed_when_lagging_benchmark(monkeypatch):
    """The end-to-end gate in main(): a lagging 'best' must not be announced.

    We drive main() with a stubbed run() whose best config is 4/4 folds but
    under the BTC benchmark, plus a previous state that had a weaker best.
    The change list must be EMPTY (suppressed), not carrying a NEW BEST line.
    """
    prev = {"rows": [_row(2, 1.0)], "bench_btc_median": 25.0}
    cur = {
        "ts": "2026-10-12T00:00:00+00:00",
        "rows": [
            _row(4, 10.0),                     # best by score: 4 folds, 10%
            {"pos_folds": 3, "median": 11.0, "n_folds": 4,
             "cfg": {k: v for k, v in rl.LIVE.items()},
             "trades": 726, "rets": [11.0] * 4},   # the live row
        ],
        "bench_btc_median": 25.0,              # BTC beat every config
        "bench_eql_median": -50.0,
        "live": rl.LIVE,
    }

    monkeypatch.setattr(rl, "run", lambda: cur)
    # Pretend a state file with `prev` already existed.
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "read_text",
                        lambda self, *a, **k: __import__("json").dumps(prev))
    monkeypatch.setattr(Path, "write_text", lambda self, *a, **k: None)

    captured = {}
    monkeypatch.setattr(rl, "STATE", ROOT / "data" / "research-loop.json")
    monkeypatch.setattr(rl, "FINDINGS",
                        ROOT / "reports" / "research-findings.md")

    def fake_send_card(*a, **k):
        raise AssertionError("no Telegram card may fire for a lagging claim")

    sys.path.insert(0, str(ROOT / "src"))
    try:
        from trading.tg import bot_ui
        monkeypatch.setattr(bot_ui, "send_card", fake_send_card)
    except Exception:
        pass

    # Capture `changed` by wrapping the print of the SUPPRESSED branch.
    out = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: out.append(
        " ".join(str(x) for x in a)))

    rl.main()

    assert any("SUPPRESSED" in line for line in out), \
        "the benchmark gate should have suppressed the lagging NEW BEST"
    assert not any("NEW BEST" in line for line in out), \
        "a lagging config must never be announced as NEW BEST"
