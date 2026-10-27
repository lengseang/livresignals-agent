"""The ingester's timeframe list must cover every timeframe the SYSTEM trades on.

WHY THIS EXISTS
---------------
Measured 2026-10-26 18:00Z. `config.TIMEFRAMES` was
`["5m", "30m", "1h", "4h", "1d"]` — 15m was simply missing. Both scheduled
ingesters (`ingest_all.py`, `ingest_alts.py`) iterate that constant, so **no
cron job had ever written a 15m bar for any symbol**. The symptom on the store:

    15m, every one of 10 symbols: stopped dead at 2026-10-26 03:15Z
    37 bars in the last 24h where 96 belong
    ingest_all.py: "OK — all series fresh"   (true — of the 5 TFs it looked at)

That is the same blind-spot class as the alt freeze: the monitor agreed with the
ingester about which series existed, and neither list contained 15m.

15m is not incidental. `unified_trader.py` gates REAL money on it:
    if tf != "15m": return False   # "reliable sample only exists on 15m"
and CORE_TFS = ["5m", "15m"]. So the one timeframe the real-money path trusts
was the one timeframe the store never refreshed. Live trading was unaffected
(the scanner fetches OHLCV live), but every replay/learner/calibrator reading
the DB was silently scoring a half-dead series — the exact failure mode
`leng-trading-stack` warns about for the alt cohort.

This test is deliberately a LIST-COMPLETENESS assertion, not a data assertion:
it fails at import time against the old config, with no fixtures and no store.
"""
from __future__ import annotations

import re
from pathlib import Path

from trading.config import TIMEFRAMES

REPO = Path(__file__).resolve().parents[1]


def test_15m_is_ingested():
    """The store must ingest 15m — it is the only TF the real-money path trusts."""
    assert "15m" in TIMEFRAMES, (
        "15m missing from config.TIMEFRAMES: no scheduled job can ever write a "
        "15m bar, and unified_trader's ELITE path requires tf == '15m'")


def test_every_known_timeframe_is_ingested():
    """No timeframe may be quietly absent from the ingest list.

    The failure mode is an OMISSION, so assert the full set rather than just 15m.
    """
    expected = {"5m", "15m", "30m", "1h", "4h", "1d"}
    assert expected <= set(TIMEFRAMES), (
        f"timeframes missing from config.TIMEFRAMES: {sorted(expected - set(TIMEFRAMES))}")


def test_ingesters_iterate_the_config_constant_not_a_local_copy():
    """Both scheduled ingesters must read config.TIMEFRAMES.

    If either hardcodes its own list, fixing config would not fix the ingest —
    a second copy of the universe is how the two lists drift apart.
    """
    for name in ("ingest_all.py", "ingest_alts.py"):
        src = (REPO / "scripts" / name).read_text()
        assert re.search(r"from trading\.config import[^\n]*TIMEFRAMES", src), \
            f"{name} does not import TIMEFRAMES from trading.config"


def test_15m_fetch_limit_can_self_heal(  ):
    """15m must be in the 200-bar limit group, not the 60-bar one.

    At limit=60 a 15m series dark >15h can never catch up from the forward
    fetch alone (60 x 15m = 15h). 200 bars = 50h of self-heal headroom.
    """
    for name in ("ingest_all.py", "ingest_alts.py"):
        src = (REPO / "scripts" / name).read_text()
        m = re.search(r'limit = 200 if tf in \(([^)]*)\)', src)
        assert m, f"{name}: could not find the fetch-limit clause"
        assert '"15m"' in m.group(1), \
            f"{name}: 15m is not in the 200-bar limit group (got {m.group(1)})"


def test_bar_seconds_covers_every_timeframe():
    """`_BAR_SECONDS` is a SECOND copy of the timeframe universe, and a partial
    copy is worse than a missing one: `check_data_health()` indexes
    FRESHNESS_DEADLINES[tf], so adding 15m to TIMEFRAMES alone raised
    KeyError: '15m' and crashed the daily health monitor (measured 2026-10-26).
    config asserts this at import; this test names the failure so it is not
    mistaken for a random import error.
    """
    from trading.config import FRESHNESS_DEADLINES
    missing = set(TIMEFRAMES) - set(FRESHNESS_DEADLINES)
    assert not missing, f"FRESHNESS_DEADLINES missing {sorted(missing)} — health monitor would KeyError"
