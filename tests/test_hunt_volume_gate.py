"""volume-confirmed entry families in hunt.py (2026-10-10).

THE SUGGESTION THIS TESTS
-------------------------
scripts/improvement_loop.py ranks as its top 'explore' idea:
    "add a volume-spike confirmation (capitulation often comes on high volume,
     not just high vol) — test vol AND volume z-score > 2 as an extra gate."

hunt.py is the system's disciplined strategy search (declared candidate list,
IS/OOS split, next-bar fills, real costs). This file pins the two new entry
families that make that suggestion testable:

  * make_dip_vol  : make_dip + a volume z-score gate
  * make_brk_vol  : make_breakout + a volume z-score gate

What these tests assert (and deliberately do NOT assert):
  + the gate is a strict SUPERSET filter — a dip_vol signal implies the base dip
    signal fired, so the extra gate can only REMOVE trades, never invent one;
  + a flat-volume window yields no signal (degenerate sd -> None -> False);
  + _vol_z is the standard population z-score of the newest bar;
  + the functions are pure and never raise on short/degenerate input.

They do NOT assert that volume adds edge — that is an empirical question for
the funnel run, and the honest answer may be no (in which case the funnel
records a dated NEGATIVE result, which is still a result).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
# hunt.py imports its siblings by bare name (from replay_engine import ...);
# loading it by path alone would fail collection, so put scripts/ on the path.
sys.path.insert(0, str(_ROOT / "scripts"))
_SPEC = importlib.util.spec_from_file_location("hunt", _ROOT / "scripts" / "hunt.py")
hunt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hunt)


class _Bar:
    """Minimal duck-typed bar: the entry functions only read .close and .volume."""
    __slots__ = ("close", "volume")

    def __init__(self, close: float, volume: float):
        self.close = close
        self.volume = volume


def _series(closes, volumes):
    return [_Bar(c, v) for c, v in zip(closes, volumes)]


def test_vol_z_is_population_zscore_of_newest_bar():
    # degenerate window (zero variance) -> None, never a divide-by-zero
    bars = _series([100] * 8, [10] * 8)
    assert hunt._vol_z(bars, 5) is None

    # real variance in the prior window: z = (newest - mean) / pop_sd
    bars = _series([100] * 7, [8, 10, 12, 10, 8, 12, 30])
    z = hunt._vol_z(bars, 4)
    # priors = [12, 10, 8, 12]; mean = 10.5; pop sd = sqrt(2.75)
    assert abs(z - (30 - 10.5) / (2.75 ** 0.5)) < 1e-9


def test_vol_z_none_when_insufficient_history():
    bars = _series([100, 101, 102], [1, 1, 1])
    assert hunt._vol_z(bars, 10) is None


def _capitulation_with_volume(vol_prev, vol_last):
    """Build a series that fires make_dip(90,35,4); vary only the volumes.

    Prices: 89 flat bars at 100, then a 4-bar decline to 90 (dd from the 90-bar
    high = -10% <= -9%, red streak = 4 >= 4, RSI on a pure decline is low).
    `vol_prev` is a LIST of the prior bars' volumes (must vary so sd > 0);
    `vol_last` is the triggering bar's volume.
    """
    closes = [100.0] * 89 + [99.0, 98.0, 97.0, 90.0]   # 93 bars
    vols = list(vol_prev) + [vol_last]
    assert len(vols) == len(closes)
    return _series(closes, vols)


def test_dip_vol_is_a_strict_superset_filter_of_dip():
    # prior volumes alternate 9/11 so the z-window has real variance.
    # closes has 93 bars, so vols must have exactly 93 entries (92 prior + spike).
    prev = ([9.0, 11.0] * 46)[:92]                       # 92 prior bars
    bars_spike = _capitulation_with_volume(prev, 40.0)
    bars_flat = _capitulation_with_volume([10.0] * 92, 10.0)  # zero variance

    base = hunt.make_dip(90, 35, 4)
    gated = hunt.make_dip_vol(90, 35, 4, vz_min=2.0, vz_win=24)

    # the base rule must fire on both (identical price path)
    assert base(bars_spike) is True
    assert base(bars_flat) is True

    # flat prior volume -> degenerate sd -> gate refuses (no invented signal)
    assert gated(bars_flat) is False
    # a real spike clears z >= 2 -> gate passes
    assert gated(bars_spike) is True

    # strict superset: whenever the gated rule fires, the base rule fires too
    for bars in (bars_spike, bars_flat):
        if gated(bars):
            assert base(bars)


def test_brk_vol_gate_uses_same_volume_logic():
    # close above the prior 20-bar high, on a volume spike. closes has 22 bars
    # (21 prior + the breakout bar); vols must match at 22 entries.
    closes = [100.0] * 21 + [105.0]
    prev = [9.0, 11.0] * 11         # 22 prior bars, varying -> sd > 0
    bars = _series(closes, prev[:21] + [50.0])
    base = hunt.make_breakout(20)
    gated = hunt.make_brk_vol(20, vz_min=2.0, vz_win=20)
    assert base(bars) is True
    assert gated(bars) is True

    flat = _series(closes, [10.0] * 21 + [50.0])
    assert base(flat) is True
    assert gated(flat) is False   # zero-variance window -> refused


def test_entry_functions_never_raise_on_tiny_input():
    tiny = _series([100.0], [1.0])
    for fn in (hunt.make_dip_vol(90, 35, 4, 2.0, 24),
               hunt.make_brk_vol(20, 2.0, 24)):
        assert fn(tiny) is False
    assert hunt._vol_z(tiny, 24) is None


def test_build_candidates_declares_volume_families():
    cands = hunt.build_candidates()
    fams = {c["fam"] for c in cands}
    assert "dip_vol" in fams and "brk_vol" in fams
    # the declared list stays small and fixed (no evolution): exact counts
    assert sum(1 for c in cands if c["fam"] == "dip_vol") == 16
    assert sum(1 for c in cands if c["fam"] == "brk_vol") == 8
