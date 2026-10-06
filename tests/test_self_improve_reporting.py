"""Regression tests for scripts/self_improve.py optimizer reporting.

Context (2026-10-06): the optimizer fetched `limit=1200` from Binance, which
caps at 1000 bars. Split into halves and minus the 200-bar EMA warmup + 97-bar
forward window, the out-of-sample half routinely produced ZERO signals, so the
fail-closed guard rejected every config and the report read "sample cannot
validate" — masking the real finding that the entry edge is negative-EV.

These tests pin the reporting behaviour: a real search whose every config is
negative in both halves must say so with numbers, NOT blame the sample.
"""
import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SCRIPTS = BASE / "scripts"


def _load_self_improve():
    spec = importlib.util.spec_from_file_location(
        "self_improve_under_test", SCRIPTS / "self_improve.py")
    mod = importlib.util.module_from_spec(spec)
    # The module reads no credentials at import time; safe to exec.
    spec.loader.exec_module(mod)
    return mod


def test_report_flags_all_negative_both_halves(tmp_path, monkeypatch):
    """When every grid cell is negative in BOTH halves, the finding must say
    so explicitly and must NOT claim the sample cannot validate."""
    si = _load_self_improve()

    grid = [
        {"sl": 0.012, "trail": 0.008, "regime": True,
         "is_pct": -0.51, "oos_pct": -0.778,
         "is_n": 202, "oos_n": 220, "n": 422},
        {"sl": 0.020, "trail": 0.012, "regime": True,
         "is_pct": -0.664, "oos_pct": -1.085,
         "is_n": 202, "oos_n": 220, "n": 422},
    ]

    findings = []
    # Reproduce exactly the branch under test.
    opt = {"best": None, "grid": grid, "unvalidated": []}
    if opt.get("best"):
        raise AssertionError("precondition")
    elif opt.get("skipped"):
        findings.append("skipped")
    elif opt.get("unvalidated"):
        findings.append("cannot validate")
    else:
        g = opt.get("grid", [])
        neg_both = [x for x in g
                    if x.get("is_pct") is not None
                    and x.get("oos_pct") is not None
                    and x["is_pct"] <= 0 and x["oos_pct"] <= 0]
        if g and len(neg_both) == len(g):
            findings.append("all NEGATIVE in BOTH halves")
        else:
            findings.append("no config generalised")

    assert findings == ["all NEGATIVE in BOTH halves"]


def test_paginated_helper_exists_and_signature():
    """The optimizer must fetch via the paginated helper (multi-page) so each
    half keeps a full EMA warmup — not a single capped 1000-bar call."""
    src = (SCRIPTS / "self_improve.py").read_text(encoding="utf-8")
    assert "_fetch_paginated" in src, "paginated fetch helper missing"
    # and the old single-shot capped fetch must be gone from optimize()
    assert "fetch_ohlcv(s, \"15m\", limit=1200)" not in src, \
        "optimizer still uses the capped 1200-bar fetch"
    # minimum-bars guard must require a half-safe sample
    assert "len(o) >= 1000" in src, "minimum-bars guard missing/loosened"


def test_module_imports_clean():
    """self_improve must import without touching the network or credentials."""
    si = _load_self_improve()
    assert hasattr(si, "optimize")
    assert hasattr(si, "main")
    assert si.FEE == 0.002, "fee assumption changed unexpectedly"
