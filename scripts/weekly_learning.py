#!/usr/bin/env python3
"""Weekly adaptive learning run: analyze accuracy, adjust params, report.

Cron: Sunday 09:30 → weekly_analysis() + params card to #reports.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading.signals import adaptive  # noqa: E402
from trading.tg import bot_ui  # noqa: E402


def run() -> dict:
    result = adaptive.weekly_analysis()
    # Always broadcast the params card (shows current state + changes).
    title, body, buttons = adaptive.params_card()
    r = bot_ui.send_card("reports", title, body, buttons)
    return {
        "adjustments": result["adjustments"],
        "params_ok": r.get("ok"),
        "resolved_signals": result["stats"].get("resolved", 0),
        "rolling_accuracy": result["stats"].get("accuracy_pct"),
    }


if __name__ == "__main__":
    res = run()
    print(json.dumps(res, indent=2))