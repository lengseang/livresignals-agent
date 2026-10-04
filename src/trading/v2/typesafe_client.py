"""The real Jev (TypeSafe System One) client: our internal request <-> `POST /v1/systemone`.

STATUS: UNVERIFIED AGAINST THE LIVE API. The wire format below comes from third-party write-ups (the vendor's own
docs are unreachable from the build sandbox); every example in tests/loop/test_v2_typesafe_client.py is labelled with
that provenance. Run `python -m trading.v2.jev_probe` once with a key to record a real response and diff it against
these assumptions. If the real shape differs, the strict translation REJECTS the reply and the adapter falls back
(`schema_violation`), which shows up as jev_share = 0 in the shadow summary: a safe failure, not a silent one.

Wire (as documented by third parties):
  POST https://api.typesafe.ai/v1/systemone      Authorization: Bearer <key>      Content-Type: application/json
  {"state": <text or JSON: our decision state WITH the feature values inlined>, "model": "jev-latest",
   "questions": {name: {"type": "choice", "instructions": str, "criteria": {option: description}}
                       | {"type": "noul",   "instructions": str}}}
  -> {"model": "jev-1.13.0",
      "answers": {name: {"type": "choice", "choice": str, "probabilities": {option: p}, "confidence": c}
                      | {"type": "noul", "noul": p}},
      "usage": {"input_tokens": n, "output_tokens": m}}
  errors: {"error": {"message": ..., "code": ...}} with 401 / 402 (code payment_required) / 422 / 429 / 529.

Our workflow's "score" questions are sent as "noul" (yes/no): the score is the probability of YES. The API's own
"score" type is NOT used: its answer is a position on a LEVEL number line (e.g. 1.43), not a 0-1 value, so a
"score"-typed reply is rejected here.

Cross-checked against search snippets of docs.typesafe.ai (not the page itself, which the sandbox blocks):
`model` is `jev-latest` (resolves to jev-1.13.0 today; `jev-preview` is newer), `state` may be a string, object or
array, a choice has up to 255 options and returns `probabilities` (sum 1) + `confidence`, where confidence is computed
from HOW SPREAD the probabilities are (one peak = high) and is described as calibrated; a 422 names the bad field.
`jev-latest` can move to a new build without notice, so the adapter counts the model that actually answered
(`stats["model:<name>"]`) and the workflow's `model` can be pinned (e.g. "jev-1.13.0") for reproducible comparisons.

Safety rules (each tested):
  * the API key is read from the environment at call time, never stored on the object, never in a repr,
    a message, a log line or a fallback reason (a real project found Jev HTTP errors echoing the key);
  * errors map to exception CLASSES (the adapter records only the class name), never to response text;
  * no retries here: the adapter's fallback IS the retry, and the hot path has a timeout budget;
  * the HTTP transport is injected, so nothing here opens a socket in tests.
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter
from typing import Optional, Protocol

from trading.v2.adapter import (
    JevBalanceExhausted, JevError, JevHttpError, JevMalformedJson, JevProtocolError,
)
from trading.v2.workflow import JevWorkflow

WIRE_URL = "https://api.typesafe.ai/v1/systemone"
KEY_ENV = "TYPESAFE_API_KEY"


class JevAuthError(JevHttpError):          # 401 / 403: do not retry
    pass


class JevRateLimited(JevHttpError):        # 429
    pass


class JevOverloaded(JevHttpError):         # 529 / 5xx
    pass


class JevBadRequest(JevHttpError):         # 422 / 400: OUR request is wrong
    pass


class JevMissingKey(JevHttpError):
    pass


class JevTransportError(JevHttpError):     # DNS, connection, TLS, timeout
    pass


class JevMissingFeatures(JevError):
    """Refused to call Jev without the feature values: it would be deciding on an opaque id."""
    category = "client_error"


class Transport(Protocol):
    def post(self, url: str, headers: dict, body: bytes, timeout_s: float) -> tuple[int, bytes]: ...


class UrllibTransport:
    """Stdlib HTTPS POST. The only place in V2 that opens a socket; tests never use it."""

    def post(self, url: str, headers: dict, body: bytes, timeout_s: float) -> tuple[int, bytes]:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:     # noqa: S310 (fixed https URL)
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:  # DNS / connect / TLS / timeout: class name only, never the message
            raise JevTransportError(type(e).__name__) from None


def build_wire_request(workflow: JevWorkflow, internal: dict, state_as_text: bool = False) -> dict:
    """Our internal request (adapter.build_request) -> the wire body."""
    workflow.require_wire_ready()
    if not isinstance(internal.get("features"), dict):
        raise JevMissingFeatures()
    if internal["features"].get("snapshot_id") != internal["state"].get("features"):
        raise JevMissingFeatures()
    questions: dict = {}
    for name, q in workflow.questions.items():
        if q.type == "choice":
            questions[name] = {"type": "choice", "instructions": q.instructions, "criteria": dict(q.criteria)}
        else:
            questions[name] = {"type": "noul", "instructions": q.instructions}
    state = {**internal["state"], "features": internal["features"]}     # the id is replaced by the values
    if internal.get("position_detail") is not None:                     # V2-15: the position workflow sees the trade
        state["position_detail"] = internal["position_detail"]
    return {"state": json.dumps(state, sort_keys=True) if state_as_text else state,
            "model": workflow.model, "questions": questions}


def _num(x, what: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not 0.0 <= x <= 1.0:
        raise JevProtocolError("schema_violation", f"{what}: expected a number in [0,1]")
    return float(x)


def translate_response(wire: object, workflow: JevWorkflow) -> dict:
    """Wire reply -> our internal raw shape (adapter.RawJevResponse). STRICT: anything unexpected is rejected."""
    if not isinstance(wire, dict):
        raise JevMalformedJson("reply is not a JSON object")
    model, answers = wire.get("model"), wire.get("answers")
    if not isinstance(model, str) or not model or not isinstance(answers, dict):
        raise JevProtocolError("schema_violation", "reply lacks model/answers")
    out: dict = {}
    for name, a in answers.items():
        if not isinstance(a, dict):
            raise JevProtocolError("schema_violation", f"{name}: answer is not an object")
        want = workflow.questions[name].type if name in workflow.questions else None
        kind = a.get("type")
        if kind == "choice":
            if want == "score":
                raise JevProtocolError("schema_violation", f"{name}: got choice, asked yes/no")
            ch, probs = a.get("choice"), a.get("probabilities")
            if not isinstance(ch, str) or not isinstance(probs, dict) or ch not in probs:
                raise JevProtocolError("schema_violation", f"{name}: choice/probabilities missing or inconsistent")
            dist = {k: _num(v, f"{name}.probabilities.{k}") for k, v in probs.items()}
            conf = a.get("confidence")
            out[name] = {"value": ch,
                         "probability": _num(conf, f"{name}.confidence") if conf is not None else dist[ch],
                         "distribution": dist}
        elif kind == "noul":
            if want == "choice":
                raise JevProtocolError("schema_violation", f"{name}: got yes/no, asked choice")
            out[name] = {"value": _num(a.get("noul"), f"{name}.noul")}
        else:
            raise JevProtocolError("schema_violation", f"{name}: unknown answer type")
    return {"model": model, "answers": out}


class TypeSafeJevClient:
    """Implements adapter.JevClient against the real API. Requires a wire-ready workflow (v2)."""

    def __init__(self, workflow: JevWorkflow, transport: Optional[Transport] = None, *, url: str = WIRE_URL,
                 state_as_text: bool = False):
        workflow.require_wire_ready()
        if not url.startswith("https://"):
            raise ValueError("the Jev URL must be https")
        self.workflow, self.transport, self.url, self.state_as_text = workflow, transport or UrllibTransport(), url, state_as_text
        self.usage: Counter = Counter()

    def __repr__(self) -> str:          # nothing secret lives on the object, and nothing is echoed
        return f"TypeSafeJevClient(workflow={self.workflow.tag}, url={self.url})"

    def evaluate(self, request: dict, timeout_s: float) -> object:
        key = os.environ.get(KEY_ENV, "").strip()
        if not key:
            raise JevMissingKey()
        body = json.dumps(build_wire_request(self.workflow, request, self.state_as_text),
                          separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "Accept": "application/json"}
        self.usage["calls"] += 1
        status, raw = self.transport.post(self.url, headers, body, timeout_s)
        self._raise_for_status(status)
        try:
            wire = json.loads(raw)
        except ValueError:
            raise JevMalformedJson("body is not JSON") from None
        usage = wire.get("usage") if isinstance(wire, dict) else None
        if isinstance(usage, dict):
            for k in ("input_tokens", "output_tokens"):
                v = usage.get(k)
                if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                    self.usage[k] += v
        self.usage["ok"] += 1
        return translate_response(wire, self.workflow)

    def _raise_for_status(self, status: int) -> None:
        self.usage[f"http_{status}"] += 1
        if status == 200:
            return
        if status == 402:
            raise JevBalanceExhausted()
        if status in (401, 403):
            raise JevAuthError()
        if status in (400, 422):
            raise JevBadRequest()
        if status == 429:
            raise JevRateLimited()
        if status == 529 or status >= 500:
            raise JevOverloaded()
        raise JevHttpError()

    def estimated_cost_usd(self, price_per_million_input_tokens: float) -> float:
        """Input tokens only (output is reported as free). The price is the owner's: sources disagree
        ($0.042 .. $0.42 per million), so there is deliberately no default."""
        return self.usage["input_tokens"] / 1e6 * price_per_million_input_tokens
