"""Record crypto headlines and Fear & Greed: python -m trading.news.run --once | --every-minutes N

Paper context data only. Each source is fetched once per cycle; a failure is written as a
fetch_error row and never stops the other sources. Polling faster than every 5 minutes is refused.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from trading.news import feeds
from trading.news.parse import FeedError, iso_utc, parse_feed, parse_fng
from trading.news.store import NewsStore

MIN_INTERVAL_MINUTES = 5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _error_row(source: str, url: str, err: Exception, fetched: datetime) -> dict:
    return {"kind": "fetch_error", "source": source, "url": url,
            "error": f"{type(err).__name__}: {str(err)[:200]}", "fetched_utc": iso_utc(fetched)}


def run_once(root: Path | str, fetcher: Callable[[str], bytes] = feeds.fetch,
             now: Callable[[], datetime] = _utc_now, store: NewsStore | None = None) -> dict:
    store = store or NewsStore(root)
    stats = {"news_new": 0, "news_seen": 0, "fng_new": 0, "errors": 0}
    for source, url in feeds.FEEDS.items():
        fetched = now()
        try:
            items = parse_feed(source, fetcher(url), fetched)
            if not items:
                raise FeedError("no items parsed (feed format change?)")
        except Exception as e:  # noqa: BLE001 - any failure is recorded as data, then we move on
            stats["errors"] += 1
            store.append([_error_row(source, url, e, fetched)])
            continue
        stats["news_seen"] += len(items)
        stats["news_new"] += store.append(items)
    fetched = now()
    try:
        stats["fng_new"] += store.append([parse_fng(fetcher(feeds.FNG_URL), fetched)])
    except Exception as e:  # noqa: BLE001
        stats["errors"] += 1
        store.append([_error_row("alternative.me", feeds.FNG_URL, e, fetched)])
    return stats


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="fetch every source once and exit")
    mode.add_argument("--every-minutes", type=float, metavar="N", help=f"poll every N minutes (N >= {MIN_INTERVAL_MINUTES})")
    ap.add_argument("--out", default="data/local/news")
    a = ap.parse_args(argv)
    if a.every_minutes is not None and a.every_minutes < MIN_INTERVAL_MINUTES:
        ap.error(f"--every-minutes must be >= {MIN_INTERVAL_MINUTES} (feed etiquette; F&G is daily)")
    root = Path(a.out)
    store = NewsStore(root)
    while True:
        started = time.monotonic()
        stats = run_once(root, store=store)
        print(f"[{iso_utc(_utc_now())}] {stats} -> {root}", flush=True)
        if a.once:
            return
        try:
            time.sleep(max(0.0, a.every_minutes * 60 - (time.monotonic() - started)))
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
