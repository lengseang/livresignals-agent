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
from typing import Annotated, Literal, Optional, Union

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
    instructions: Optional[str] = None          # what is asked; required to talk to the real API
    criteria: Optional[dict[str, str]] = None   # option -> what it means; keys must equal `options`

    @model_validator(mode="after")
    def _unique(self):
        if len(set(self.options)) != len(self.options):
            raise ValueError("duplicate options")
        if self.criteria is not None and set(self.criteria) != set(self.options):
            raise ValueError("criteria keys must equal options")
        return self


class ScoreQuestion(BaseModel):
    """A 0-1 score. On the wire it is a yes/no ("noul") question: the score is the probability of YES."""
    model_config = _STRICT
    type: Literal["score"]
    instructions: Optional[str] = None


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

    def require_wire_ready(self) -> None:
        """The real API needs the question text. A workflow without it cannot be sent (workflow v1 cannot)."""
        for name, q in self.questions.items():
            if not (q.instructions or "").strip():
                raise ValueError(f"question {name!r} has no instructions: not usable against the real API")
            if q.type == "choice" and not q.criteria:
                raise ValueError(f"choice question {name!r} has no criteria: not usable against the real API")

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


# ---------------------------------------------------------------- V2-15: the POSITION workflow (asked while a trade is open)

POSITION_ACTIONS = ("HOLD", "TIGHTEN_STOP", "REDUCE", "EXIT")
REQUIRED_POSITION_QUESTIONS = {
    "position_action": "choice", "momentum_fading": "score", "reversal_risk": "score", "target_reachable": "score",
}
POSITION_WORKFLOW = "crypto_position_v1"


class PositionWorkflow(BaseModel):
    """Same shape and wire rules as JevWorkflow; a different, smaller question set. Its actions can only reduce risk."""
    model_config = _STRICT
    workflow_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    version: VersionTag
    model: Annotated[str, Field(min_length=1)]
    questions: dict[str, Question]

    @model_validator(mode="after")
    def _complete(self):
        if set(self.questions) != set(REQUIRED_POSITION_QUESTIONS):
            raise ValueError(f"position workflow must ask exactly {sorted(REQUIRED_POSITION_QUESTIONS)}")
        for name, kind in REQUIRED_POSITION_QUESTIONS.items():
            if self.questions[name].type != kind:
                raise ValueError(f"question {name!r} must be a {kind}")
        if tuple(sorted(self.questions["position_action"].options)) != tuple(sorted(POSITION_ACTIONS)):
            raise ValueError(f"position_action options must be exactly {POSITION_ACTIONS}")
        return self

    require_wire_ready = JevWorkflow.require_wire_ready
    tag = JevWorkflow.tag


def load_position_workflow(directory: Path | str = WORKFLOW_DIR) -> PositionWorkflow:
    return PositionWorkflow.model_validate_json((Path(directory) / f"{POSITION_WORKFLOW}.json").read_text(encoding="utf-8"))
