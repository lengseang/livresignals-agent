"""Every reader that calls the trend book's mean a per-trade result.

Evidence for T0.22 (2026-11-12). READ-ONLY.

WHY
---
`data/trend-state.json` `log` holds 212 closed rows. The mean of `ret` is
+1.21% (net +1.05%) and the win rate is 21.7%. `research/_trend_tail_concentration.py`
shows the mean is five trades deep: delete the five best and the NET mean is
+0.003%, and every one of the top 10 is 26-86% — sizes a spot trend follower
does not hold for weeks against a -15% hard stop. The median is -1.32% NET.

The `ret` field IS a position return, not a per-dollar mean: that is only
sound if `ret` is what a position made. The question this probe answers: which
scripts read this book, and do any of them present the arithmetic mean as a
typical trade or as evidence of an edge?

Method: grep every `.py` under `scripts/` and `src/` for the state file and for
`trend_follower` in a SOURCES/tuple list; print the file, line and text. No
verdicts are assigned here — the design note does that.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path("/opt/trading-agent")
PATTERNS = ("trend-state.json", "trend_follower")
SKIP = ("__pycache__", "venv", "/tests/", "test_")


def main() -> None:
    hits = 0
    for base in ("scripts", "src", "research"):
        for p in sorted((ROOT / base).rglob("*.py")):
            s = str(p)
            if any(k in s for k in SKIP):
                continue
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(lines, 1):
                if any(re.search(pat, line) for pat in PATTERNS):
                    print(f"{s.replace(str(ROOT)+'/', ''):45s}:{i:<5d} {line.strip()[:90]}")
                    hits += 1
    print(f"\n{hits} reference(s). A reader that divides the log's sum by n is "
          "reporting the mean of 212 position returns; if it labels that "
          "'per trade' or 'expectancy' without the median beside it, it is "
          "quoting a 5-trade tail as the strategy.")


if __name__ == "__main__":
    main()
