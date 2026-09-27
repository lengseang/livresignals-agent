"""P0c: the candidate cards stay honest (research/candidates/candidates.json)."""
from __future__ import annotations

import json
from pathlib import Path

from trading.replay import gate

CARDS = json.loads((Path(__file__).resolve().parents[2] / "research" / "candidates" / "candidates.json")
                   .read_text(encoding="utf-8"))
REQUIRED = {"id", "strategy", "family", "stage", "sources", "claim", "rules", "our_use", "data",
            "cost_sensitivity", "license", "prior", "trial_budget"}
STAGES = ["card", "rules_read", "pre_registered", "validated", "rejected", "shadow", "promoted"]


def test_every_card_is_complete_and_unique():
    ids = [c["id"] for c in CARDS["candidates"]]
    assert len(ids) == len(set(ids))
    for c in CARDS["candidates"]:
        assert REQUIRED <= set(c), c["id"]
        assert c["stage"] in STAGES, c["id"]


def test_family_is_the_gate_family_fixed_before_results():
    for c in CARDS["candidates"]:
        assert gate.family_of(c["strategy"]) == c["family"], c["id"]


def test_no_card_past_the_card_stage_has_unknown_rules():
    # params may not be invented: a card is pre-registered only after its rules are read from the source
    for c in CARDS["candidates"]:
        if STAGES.index(c["stage"]) >= STAGES.index("pre_registered"):
            assert c["rules"]["status"] == "KNOWN", c["id"]


def test_every_non_benchmark_card_cites_a_source_url():
    for c in CARDS["candidates"]:
        if c["family"] != "benchmark":
            assert c["sources"] and all(s["url"].startswith("https://") for s in c["sources"]), c["id"]
