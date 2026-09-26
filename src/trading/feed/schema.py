"""Validate feed payloads against the published JSON Schema files in docs/feed/schema/v1/.

`jsonschema` is not installed and adding a dependency for this is not worth it, so this implements the subset
of JSON Schema (2020-12) the v1 files use: type (incl. lists and "integer"), enum, const, required, properties,
additionalProperties (bool or schema), items, $ref to "#/$defs/<name>" or "<file>.json#/$defs/<name>",
anyOf, minimum. Any other keyword in a schema file is an error, so the files can't silently promise more
than is checked.
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "docs" / "feed" / "schema" / "v1"
_KNOWN = {"$schema", "$id", "$defs", "$comment", "title", "description", "type", "enum", "const", "required",
          "properties", "additionalProperties", "items", "$ref", "anyOf", "minimum", "examples"}
EVENT_TYPES = ("hello", "heartbeat", "quotes", "signal", "paper_fill", "position", "strategy_status",
               "data_quality", "resync")
_cache: dict[str, dict] = {}


def load_schemas(directory: Path = SCHEMA_DIR) -> dict[str, dict]:
    if not _cache:
        for f in sorted(directory.glob("*.json")):
            _cache[f.stem] = json.loads(f.read_text(encoding="utf-8"))
    return _cache


def _type_ok(x, t: str) -> bool:
    if t == "null":
        return x is None
    if t == "boolean":
        return isinstance(x, bool)
    if t == "integer":
        return isinstance(x, int) and not isinstance(x, bool)
    if t == "number":
        return isinstance(x, (int, float)) and not isinstance(x, bool)
    if t == "string":
        return isinstance(x, str)
    if t == "array":
        return isinstance(x, list)
    if t == "object":
        return isinstance(x, dict)
    raise ValueError(f"unknown type {t!r}")


def _resolve(ref: str, root: dict) -> tuple[dict, dict]:
    file, _, frag = ref.partition("#")
    doc = load_schemas()[file[:-5]] if file else root
    node = doc
    for part in [p for p in frag.split("/") if p]:
        node = node[part]
    return node, doc


def check(x, s: dict, root: dict, path: str = "$") -> None:
    unknown = set(s) - _KNOWN
    if unknown:
        raise ValueError(f"schema keyword(s) {sorted(unknown)} not supported by trading.feed.schema")
    if "$ref" in s:
        node, doc = _resolve(s["$ref"], root)
        check(x, node, doc, path)
    if "anyOf" in s:
        errs = []
        for sub in s["anyOf"]:
            try:
                check(x, sub, root, path)
                break
            except ValueError as e:
                errs.append(str(e))
        else:
            raise ValueError(f"{path}: matches none of anyOf: {errs}")
    if "type" in s:
        types = s["type"] if isinstance(s["type"], list) else [s["type"]]
        if not any(_type_ok(x, t) for t in types):
            raise ValueError(f"{path}: {x!r} is not of type {types}")
    if "const" in s and x != s["const"]:
        raise ValueError(f"{path}: {x!r} != const {s['const']!r}")
    if "enum" in s and x not in s["enum"]:
        raise ValueError(f"{path}: {x!r} not in {s['enum']}")
    if "minimum" in s and isinstance(x, (int, float)) and not isinstance(x, bool) and x < s["minimum"]:
        raise ValueError(f"{path}: {x!r} < minimum {s['minimum']}")
    if isinstance(x, dict):
        for k in s.get("required", []):
            if k not in x:
                raise ValueError(f"{path}: missing required {k!r}")
        props = s.get("properties", {})
        for k, v in x.items():
            if k in props:
                check(v, props[k], root, f"{path}.{k}")
            else:
                ap = s.get("additionalProperties", True)
                if ap is False:
                    raise ValueError(f"{path}: unexpected property {k!r}")
                if isinstance(ap, dict):
                    check(v, ap, root, f"{path}.{k}")
    if isinstance(x, list) and "items" in s:
        for i, v in enumerate(x):
            check(v, s["items"], root, f"{path}[{i}]")


def validate(x, name: str) -> None:
    s = load_schemas()[name]
    check(x, s, s)


def validate_envelope(env: dict) -> None:
    """Envelope, then its data against the schema of its type. Raises ValueError."""
    validate(env, "envelope")
    validate(env["data"], env["type"])


def validate_rest(name: str, payload) -> None:
    validate(payload, name)
