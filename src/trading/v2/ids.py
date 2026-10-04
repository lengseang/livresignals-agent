"""Deterministic ids and hashes: same inputs, same id. No clock, no randomness."""
from __future__ import annotations

import hashlib


def derive_id(prefix: str, *parts: object) -> str:
    """`<prefix>_<26 upper-hex chars>` from the parts. Matches the contracts' id patterns."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return f"{prefix}_{digest[:26].upper()}"
