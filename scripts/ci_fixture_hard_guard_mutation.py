#!/usr/bin/env python3
"""M0-04 CI fixture: hard-guard mutation set must be caught.

Proves that a mutation weakening an immutable risk rule is detected.
"""
import sys
from dataclasses import dataclass

# Canonical immutable guards (do not weaken).
@dataclass(frozen=True)
class Guards:
    max_risk_fraction: float = 0.02   # 2% loss-at-stop ceiling
    max_stop_distance: float = 0.05   # 5% stop distance
    daily_loss_halt: float = 0.05     # -5% daily halt
    weekly_loss_halt: float = 0.10    # -10% weekly halt
    max_drawdown_halt: float = 0.15   # -15% emergency


REQUIRED = {
    "max_risk_fraction": 0.02,
    "max_stop_distance": 0.05,
    "daily_loss_halt": 0.05,
    "weekly_loss_halt": 0.10,
    "max_drawdown_halt": 0.15,
}


def check(guards: Guards) -> list[str]:
    findings = []
    for name, expected in REQUIRED.items():
        actual = getattr(guards, name)
        if actual != expected:
            findings.append(f"{name} mutated: {actual} != {expected}")
        if actual > expected:
            findings.append(f"{name} WEAKENED (greater than immutable value)")
    return findings


def main() -> int:
    baseline = Guards()
    findings = check(baseline)
    if findings:
        print("FAIL: baseline guard set is invalid")
        return 1

    # Simulate a survived mutation (a bad edit weakening the stop distance).
    mutated = Guards(max_stop_distance=0.08)
    findings = check(mutated)
    if findings:
        print("PASS: mutation detected ->", findings[0])
        return 0

    print("FAIL: mutation NOT detected")
    return 1


if __name__ == "__main__":
    sys.exit(main())