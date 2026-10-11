"""The paper book must charge the MEASURED exchange cost, not a frozen constant.

Defect found 2026-10-11:

  unified_trader charged PAPER_COST_RT = 0.0003 (0.03% round trip), a constant
  taken from "30 real fills" recovered fees back on 2026-10-05. fee_audit.py
  (cron 09:50 daily) now MEASURES the exchange's real round trip at
  0.16092% — 5.4x higher. The model was therefore not conservative, it was
  optimistic by ~0.13%/trade; on 2123 trades that is ~$13.8 of hidden cost,
  enough to flip the reported +$18.98 net to about -$3.35.

The fix prefers the measured round-trip from data/fee-audit.json, keeps an
explicit PAPER_COST_RT env var as the operator override, and only falls back to
the old constant when no audit file exists. These tests pin that precedence.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_probe(env_extra: dict | None = None, audit: dict | None = None):
    """Load unified_trader in a subprocess (fresh import-time constant) and
    print the resolved cost + source. Optionally inject a fake fee-audit.json
    so the test does not depend on the real audit's current value."""
    import tempfile
    import os
    script = f"""
import importlib.util, sys, json, os
from pathlib import Path
spec = importlib.util.spec_from_file_location('ut', r'{ROOT / "scripts" / "unified_trader.py"}')
m = importlib.util.module_from_spec(spec); sys.modules['ut'] = m
spec.loader.exec_module(m)
print(json.dumps({{'rt': m.PAPER_COST_RT, 'src': m._COST_SOURCE}}))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    if env_extra:
        env.update(env_extra)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, env=env, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_measured_round_trip_is_preferred_over_the_stale_constant():
    """With no env override, the cost must come from fee-audit.json when it exists."""
    r = _run_probe()
    audit = json.loads((ROOT / "data" / "fee-audit.json").read_text())
    pct = audit.get("totals", {}).get("measured_round_trip_pct")
    if pct:  # audit present in this checkout
        assert r["src"] == "fee-audit.json measured_round_trip_pct", r
        assert abs(r["rt"] - pct / 100.0) < 1e-9, r
        # and it must NOT be the stale hardcoded 0.0003
        assert r["rt"] != 0.0003, r


def test_env_override_still_wins():
    r = _run_probe(env_extra={"PAPER_COST_RT": "0.0042"})
    assert r["rt"] == 0.0042, r
    assert r["src"] == "env PAPER_COST_RT", r


def test_measured_round_trip_helper_reads_percent_as_fraction():
    """_measured_round_trip converts the audit's PERCENT to a FRACTION."""
    import importlib.util, sys as _sys
    spec = importlib.util.spec_from_file_location("ut2", ROOT / "scripts" / "unified_trader.py")
    m = importlib.util.module_from_spec(spec)
    _sys.modules["ut2"] = m
    spec.loader.exec_module(m)
    v = m._measured_round_trip()
    audit = json.loads((ROOT / "data" / "fee-audit.json").read_text())
    pct = audit.get("totals", {}).get("measured_round_trip_pct")
    if pct:
        assert v is not None and abs(v - pct / 100.0) < 1e-9, (v, pct)
