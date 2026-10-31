"""A rehearsal must leave no trace, and nobody may close another's position.

Two defects found on 2026-10-31 while verifying the ownership-guard fix, both
observed on the LIVE demo runner rather than in a test:

1. `run_once(dry=True)` did not advance `last_bar` (deliberate, documented) but
   still called `save_state(st)` unconditionally. So a `--dry-run` persisted its
   ADOPTIONS and derived stop/qty/symbol. Running one dry pass handed
   ETH_TURTLE_H4 a copy of the position RANDOM_ENTRY_CONTROL owns (entry
   2241.49, ledger_id None) -- re-creating the exact double-claim that 294d5ae
   had just removed.

2. Nothing stopped a non-owner from exiting. `decide()` returning "close" leads
   to a market Sell for the whole position on the SYMBOL, so a strategy carrying
   an unowned position would close the real owner's position and book the P&L to
   the wrong arm of the experiment.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO = Path("/opt/trading-agent")
RUNNER = REPO / "scripts" / "run_crypto_trend_demo.py"
STATE = REPO / "state" / "crypto-trend-demo" / "state.json"


def _src() -> str:
    return RUNNER.read_text()


def _fn(name: str):
    """Parse the runner and return the AST of a top-level function."""
    for node in ast.walk(ast.parse(_src())):
        if isinstance(node, (ast.FunctionDef,)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {RUNNER.name}")


def test_dry_run_does_not_persist_state():
    """save_state() must be guarded by `not dry` inside run_once()."""
    fn = _fn("run_once")
    guards = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == "save_state":
            guards.append(node)
    assert guards, "run_once no longer calls save_state at all"
    for call in guards:
        # Every save_state call must sit inside an `if not dry:` body.
        inside = False
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and _is_not_dry(node.test):
                if any(sub is call for sub in ast.walk(node)):
                    inside = True
        assert inside, (
            "save_state() is reachable on a dry run; a rehearsal would "
            "persist its adoptions to the live state file"
        )


def _is_not_dry(test: ast.expr) -> bool:
    return (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
            and test.operand.id == "dry")


def test_close_is_guarded_by_ownership():
    """The close branch must refuse when no committed ledger id proves ownership."""
    fn = _fn("run_once")
    src = ast.get_source_segment(_src(), fn) or ""
    assert "close_refused_unowned" in src, (
        "the exit path has no ownership check; a strategy holding an adopted "
        "position could market-Sell the real owner's position"
    )
    # And the guard must key on proof of ownership, not on in_pos alone.
    assert "ledger_id" in src and "owns_exchange_position" in src


def test_live_state_has_no_unowned_double_claim():
    """The real state must not carry two strategies claiming one symbol."""
    if not STATE.exists():
        pytest.skip("live demo runner state not present")
    st = json.loads(STATE.read_text())
    claims: dict[str, list[str]] = {}
    for sid, s in st.get("strategies", {}).items():
        s = s or {}
        if s.get("in_pos") and s.get("symbol"):
            claims.setdefault(s["symbol"], []).append(sid)
    # A matched treatment/control pair may legitimately both hold the same
    # symbol; two strategies with no such link may not.
    for sym, sids in claims.items():
        unowned = [x for x in sids
                   if not ((st["strategies"][x] or {}).get("ledger_id")
                           or (st["strategies"][x] or {}).get("owns_exchange_position"))]
        assert not unowned, (
            f"{unowned} hold {sym} with no committed ledger_id and no ownership "
            f"flag -- an unowned claim on a real exchange position"
        )
