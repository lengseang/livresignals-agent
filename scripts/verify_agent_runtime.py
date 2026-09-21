#!/usr/bin/env python3
"""M0-03 runtime verification: all worker profiles use OPENCODE-ZEN with ZEN-2..10 fallback.

Outputs reports/office/zen-runtime.json with per-profile results.
Exits nonzero if any profile violates the ZEN policy.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

PROFILES = Path(r"Z:\Hermes\home\profiles")
EXPECTED_PRIMARY = "custom:opencode-zen"
EXPECTED_FALLBACKS = [f"custom:opencode-zen-{n}" for n in range(2, 11)]


def check_profile(profile: Path) -> dict:
    config = profile / "config.yaml"
    result = {"worker": profile.name, "ok": False, "findings": []}

    if not config.exists():
        result["findings"].append("missing config.yaml")
        return result

    data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    model = data.get("model") or {}
    provider = (model.get("provider") or "").lower()
    fallbacks = [str(x).lower() for x in (data.get("fallback_providers") or [])]

    if provider != EXPECTED_PRIMARY:
        result["findings"].append(f"model.provider={provider!r} expected {EXPECTED_PRIMARY!r}")
    else:
        result["provider"] = provider

    missing = [f for f in EXPECTED_FALLBACKS if f not in fallbacks]
    if missing:
        result["findings"].append(f"missing fallback(s): {missing}")

    paid = [f for f in fallbacks if "ve-" in f or "vectorengine" in f]
    if paid:
        result["findings"].append(f"paid fallback(s) present: {paid}")

    cap = data.get("max_concurrent_sessions")
    if cap is not None:
        result["findings"].append(f"max_concurrent_sessions={cap!r} expected null (unlimited per profile)")

    result["ok"] = not result["findings"]
    return result


def main() -> int:
    results = []
    fatal = 0
    for profile in sorted(PROFILES.iterdir()):
        if not profile.is_dir() or not (profile / "config.yaml").exists():
            continue
        r = check_profile(profile)
        results.append(r)
        if not r["ok"]:
            fatal += 1

    report = {
        "checked_at_utc": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "policy": {"primary": EXPECTED_PRIMARY, "fallbacks": EXPECTED_FALLBACKS, "paid_in_automation": "forbidden"},
        "profiles_checked": len(results),
        "profiles_failed": fatal,
        "profiles": results,
    }

    out = Path(r"Z:\Hermes\home\trading-agent\reports\office\zen-runtime.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"checked": len(results), "failed": fatal, "report": str(out)}, indent=2))

    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())