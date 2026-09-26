"""Stream Binance monthly kline zips (data.binance.vision layout) as Bars, one row at a time.

Columns: open_time, open, high, low, close, volume, close_time, quote_volume, trades,
taker_buy_base, taker_buy_quote, ignore. No full-file load: each zip member is read as a stream.

end_ms is Binance's close_time + 1, not open + 1m. Measured on BTCUSDT and ETHUSDT (2017-08..2026-08),
per symbol: 21,602 bars of 2017-2018 are phase-shifted off the minute grid (e.g. open 1512367220799,
close 1512367280798); 18 bars have a close_time other than open + 59,999 ms, mostly the last bar
before an exchange halt (close_time = the halt instant). Using the real close keeps "known at end_ms"
true. One of those 18 (2020-12-21) is a zero-trade placeholder whose close_time precedes its
open_time; such rows carry no trade and are dropped and counted, never repaired. Any other row whose
close_time is outside its bar raises.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path

from trading.replay.bars import INTERVAL_MS, Bar, to_ms

LATE_CLOSE_MS = 2_000      # tolerance for the late-close rows dropped below; anything later still raises
_MONTH = re.compile(r"^(?P<sym>[A-Z0-9]+)-(?P<iv>\w+)-(?P<ym>\d{4}-\d{2})\.zip$")


def iter_klines(path: Path, symbol: str, interval: str = "1m", stats: dict | None = None) -> Iterator[Bar]:
    """Bars from one zip. `stats`, if given, counts rows dropped as zero-trade placeholders."""
    step = INTERVAL_MS[interval]
    with zipfile.ZipFile(path) as z:
        (member,) = [n for n in z.namelist() if n.endswith(".csv")]
        with z.open(member) as raw:
            for r in csv.reader(io.TextIOWrapper(raw, encoding="ascii", newline="")):
                if not r or not r[0].isdigit():
                    continue                                    # header row (some files have one)
                open_ms = to_ms(int(r[0]))
                end_ms = to_ms(int(r[6])) + 1          # the instant the bar is known (real, not grid)
                if end_ms <= open_ms and int(r[8]) == 0 and float(r[5]) == 0:
                    if stats is not None:
                        stats[f"{symbol}:dropped_placeholder"] = stats.get(f"{symbol}:dropped_placeholder", 0) + 1
                    continue
                if open_ms + step + 1 < end_ms <= open_ms + step + LATE_CLOSE_MS:
                    # a traded bar closing up to 2 s late (T3.4 scan: 5 rows in 15 coins, e.g. LTCUSDT
                    # 2018-02-11 04:00). Its real end overlaps the next bar and a clamped end would leak the
                    # future, so it is dropped (a 1-minute gap) and counted, never repaired.
                    if stats is not None:
                        stats[f"{symbol}:dropped_late_close"] = stats.get(f"{symbol}:dropped_late_close", 0) + 1
                    continue
                if not open_ms < end_ms <= open_ms + step + 1:
                    raise ValueError(f"{path.name}: close_time {r[6]} is not within a {interval} bar at {r[0]}")
                yield Bar(symbol, interval, open_ms, end_ms,
                          float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                          float(r[5]), float(r[7]), int(r[8]), float(r[9]))


def month_files(root: Path, symbol: str, interval: str = "1m") -> list[tuple[str, Path]]:
    """[(YYYY-MM, path)] sorted by month, from <root>/klines_<iv>/<SYM>/."""
    d = Path(root) / f"klines_{interval}" / symbol
    out = []
    for p in d.glob(f"{symbol}-{interval}-*.zip"):
        m = _MONTH.match(p.name)
        if m and m["sym"] == symbol:
            out.append((m["ym"], p))
    return sorted(out)


def _month_bounds(ym: str) -> tuple[int, int]:
    from datetime import datetime, timezone
    y, m = map(int, ym.split("-"))
    a = datetime(y, m, 1, tzinfo=timezone.utc)
    b = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=timezone.utc)
    return int(a.timestamp() * 1000), int(b.timestamp() * 1000)


def iter_symbol(root: Path, symbol: str, start_ms: int | None = None, end_ms: int | None = None,
                interval: str = "1m", stats: dict | None = None) -> Iterator[Bar]:
    """All bars of one symbol in time order with start_ms <= open_ms and end_ms(bar) <= end_ms.

    Rows must be strictly increasing; anything else raises (a silently re-ordered history is a
    look-ahead risk). Missing months are simply absent: gaps are data, never interpolated.
    """
    last = -1
    for ym, path in month_files(root, symbol, interval):
        m0, m1 = _month_bounds(ym)
        if (end_ms is not None and m0 >= end_ms) or (start_ms is not None and m1 <= start_ms):
            continue
        for bar in iter_klines(path, symbol, interval, stats):
            if bar.open_ms <= last:
                raise ValueError(f"{path.name}: open_time {bar.open_ms} not after {last}")
            last = bar.open_ms
            if start_ms is not None and bar.open_ms < start_ms:
                continue
            if end_ms is not None and bar.end_ms > end_ms:
                return
            yield bar
