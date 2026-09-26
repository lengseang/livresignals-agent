"""Stream Binance monthly kline zips (data.binance.vision layout) as Bars, one row at a time.

Columns: open_time, open, high, low, close, volume, close_time, quote_volume, trades,
taker_buy_base, taker_buy_quote, ignore. No full-file load: each zip member is read as a stream.

end_ms is Binance's close_time + 1, not open + 1m. Measured on BTCUSDT and ETHUSDT (2017-08..2026-08),
per symbol: 21,602 bars of 2017-2018 are phase-shifted off the minute grid (e.g. open 1512367220799,
close 1512367280798); 18 bars have a close_time other than open + 59,999 ms, mostly the last bar
before an exchange halt (close_time = the halt instant). Using the real close keeps "known at end_ms"
true. One of those 18 (2020-12-21) is a zero-trade placeholder whose close_time precedes its
open_time; such rows carry no trade and are dropped and counted, never repaired.

Measured across all 15 coins (T3.3c, 1,268 zips): 5 rows close 132-1,539 ms AFTER their minute, each the
last bar before an exchange halt (e.g. LTCUSDT 2018-02-11, LINK/NEAR/SOL 2021-04-25). They carry real
trades, so they are kept at their real close and counted as `late_close`. One of them (SOLUSDT
2021-12-24 04:59) is followed by a bar that opens 475 ms before that close; a bar cannot end after the
next one started, so iter_symbol drops that row and counts it (`dropped_late_close_overlap`). Any row
closing more than one extra interval late, or before its open with trades, raises.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path

from trading.replay.bars import INTERVAL_MS, Bar, to_ms

_MONTH = re.compile(r"^(?P<sym>[A-Z0-9]+)-(?P<iv>\w+)-(?P<ym>\d{4}-\d{2})\.zip$")


_COLS = ("open_raw", "open", "high", "low", "close", "volume", "close_raw", "quote_volume", "trades", "taker_buy")


def _rows(path: Path) -> list[tuple]:
    """Parsed rows of one zip, via a binary cache next to it (built once by this same parser).

    The cache stores the exact Python values the CSV parser produced (float64/int64 round-trip them
    exactly), so cached and uncached replays are identical. It is rebuilt if the zip's size or
    mtime changes.
    """
    import numpy as np
    cache = path.with_suffix(".npz")
    st = path.stat()
    stamp = np.array([st.st_size, st.st_mtime_ns], dtype=np.int64)
    if cache.exists():
        try:
            with np.load(cache) as z:
                if np.array_equal(z["stamp"], stamp):
                    return list(zip(*(z[c].tolist() for c in _COLS)))
        except (OSError, ValueError, KeyError):
            pass                                                # unreadable cache: rebuild below
    rows = []
    with zipfile.ZipFile(path) as z:
        (member,) = [n for n in z.namelist() if n.endswith(".csv")]
        with z.open(member) as raw:
            for r in csv.reader(io.TextIOWrapper(raw, encoding="ascii", newline="")):
                if not r or not r[0].isdigit():
                    continue                                    # header row (some files have one)
                rows.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]),
                             int(r[6]), float(r[7]), int(r[8]), float(r[9])))
    if rows:
        cols = list(zip(*rows))
        arrays = {c: np.array(v, dtype=np.int64 if c in ("open_raw", "close_raw", "trades") else np.float64)
                  for c, v in zip(_COLS, cols)}
        # Parallel replays may build the same cache at once: each writes its own temp file, and losing
        # the race is harmless (the winner wrote identical bytes from the same zip). A failed write only
        # means this run didn't cache; the parsed rows are still returned.
        import os
        import uuid
        tmp = cache.with_name(f"{cache.stem}.{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp.npz")
        try:
            np.savez(tmp, stamp=stamp, **arrays)
            tmp.replace(cache)
        except OSError:
            pass
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return rows


def iter_klines(path: Path, symbol: str, interval: str = "1m", stats: dict | None = None) -> Iterator[Bar]:
    """Bars from one zip. `stats`, if given, counts rows dropped as zero-trade placeholders."""
    step = INTERVAL_MS[interval]
    for r in _rows(path):
        open_ms = to_ms(r[0])
        end_ms = to_ms(r[6]) + 1               # the instant the bar is known (real, not grid)
        if end_ms <= open_ms and r[8] == 0 and r[5] == 0:
            if stats is not None:
                stats[f"{symbol}:dropped_placeholder"] = stats.get(f"{symbol}:dropped_placeholder", 0) + 1
            continue
        if open_ms + step + 1 < end_ms <= open_ms + 2 * step:
            # last bar before a halt closing just past its minute (5 measured rows, 132-1,539 ms):
            # real trades, kept at their real close and counted; never moved onto the grid
            if stats is not None:
                stats[f"{symbol}:late_close"] = stats.get(f"{symbol}:late_close", 0) + 1
        elif not open_ms < end_ms <= open_ms + step + 1:
            raise ValueError(f"{path.name}: close_time {r[6]} is not within a {interval} bar at {r[0]}")
        yield Bar(symbol, interval, open_ms, end_ms, r[1], r[2], r[3], r[4], r[5], r[7], r[8], r[9])


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
    step = INTERVAL_MS[interval]
    held: Bar | None = None          # a late-closing bar, yielded only once the next bar shows it does not overlap
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
            if held is not None:
                if bar.open_ms < held.end_ms:
                    if stats is not None:
                        k = f"{symbol}:dropped_late_close_overlap"
                        stats[k] = stats.get(k, 0) + 1
                else:
                    yield held
                held = None
            if end_ms is not None and bar.end_ms > end_ms:
                return
            if bar.end_ms > bar.open_ms + step + 1:
                held = bar
                continue
            yield bar
    if held is not None:
        yield held
