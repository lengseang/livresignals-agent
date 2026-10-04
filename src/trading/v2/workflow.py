"""The Jev workflow definition: OUR question set, versioned, separate from TypeSafe's wire format.

The adapter translates this into whatever the live API needs; nothing here assumes the
TypeSafe request shape. A workflow is data (JSON), so Hermes can PROPOSE a new one
(CHANGE_JEV_WORKFLOW) without touching code.

Every workflow must define the questions a Decision is built from; a workflow that
cannot fill a Decision is rejected at load time, not at 3am.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading.v2.contracts import VersionTag

ACTIONS = ("LONG", "SHORT", "HOLD", "EXIT", "REDUCE", "NO_TRADE")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "EXTREME")
# Questions every workflow must ask: they populate Decision.signals / .questions / .risk_level.
REQUIRED_QUESTIONS = {
    "market_regime": "choice", "trend_quality": "score", "breakout_probability": "score",
    "reversal_probability": "score", "entry_quality": "score", "expected_edge": "score",
    "timing_quality": "score", "risk_level": "choice", "action": "choice",
}
WORKFLOW_DIR = Path(__file__).parent / "workflows"

_STRICT = ConfigDict(extra="forbid", strict=True, frozen=True)


class ChoiceQuestion(BaseModel):
    model_config = _STRICT
    type: Literal["choice"]
    options: Annotated[list[str], Field(min_length=2)]

    @model_validator(mode="after")
    def _unique(self):
        if len(set(self.options)) != len(self.options):
            raise ValueError("duplicate options")
        return self


class ScoreQuestion(BaseModel):
    model_config = _STRICT
    type: Literal["score"]


Question = Union[ChoiceQuestion, ScoreQuestion]


class JevWorkflow(BaseModel):
    model_config = _STRICT
    workflow_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    version: VersionTag
    model: Annotated[str, Field(min_length=1)]
    questions: dict[str, Question]

    @model_validator(mode="after")
    def _fills_a_decision(self):
        for name, kind in REQUIRED_QUESTIONS.items():
            q = self.questions.get(name)
            if q is None:
                raise ValueError(f"workflow lacks required question {name!r}")
            if q.type != kind:
                raise ValueError(f"question {name!r} must be a {kind}, got {q.type}")
        if tuple(sorted(self.questions["action"].options)) != tuple(sorted(ACTIONS)):
            raise ValueError(f"action options must be exactly {ACTIONS}")
        if tuple(sorted(self.questions["risk_level"].options)) != tuple(sorted(RISK_LEVELS)):
            raise ValueError(f"risk_level options must be exactly {RISK_LEVELS}")
        return self

    @property
    def tag(self) -> str:
        """`<workflow_id>-<version>`: what Decision.workflow_version records."""
        return f"{self.workflow_id}-{self.version}"


def load_workflow(path: Path | str) -> JevWorkflow:
    return JevWorkflow.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_active(directory: Path | str = WORKFLOW_DIR) -> JevWorkflow:
    """Follow `active.json` ({"workflow": "<file stem>"}) like the note's `active -> v2` link."""
    directory = Path(directory)
    name = json.loads((directory / "active.json").read_text(encoding="utf-8"))["workflow"]
    return load_workflow(directory / f"{name}.json")
