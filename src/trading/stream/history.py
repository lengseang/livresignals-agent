"""Backfill public daily market data from data.binance.vision (paper research only).

Every file is checked against Binance's published SHA-256 before it is kept; a
file that fails is deleted, never used. Spot bookTicker is not published
historically, so spreads come only from the live recorder (run_ingest).

    python -m trading.stream.history --days 30                              # daily files
    python -m trading.stream.history --monthly klines_1m --since 2017-08   # full history
"""
from __future__ import annotations

import argparse
import hashlib
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

BASE = "https://data.binance.vision/data/spot/daily"
KINDS = {
    "aggTrades": "aggTrades/{sym}/{sym}-aggTrades-{d}.zip",
    "klines_1s": "klines/{sym}/1s/{sym}-1s-{d}.zip",
}


MONTHLY_BASE = "https://data.binance.vision/data/spot/monthly"
MONTHLY_KINDS = {
    "klines_1m": "klines/{sym}/1m/{sym}-1m-{m}.zip",
    "klines_1s": "klines/{sym}/1s/{sym}-1s-{m}.zip",
    "aggTrades": "aggTrades/{sym}/{sym}-aggTrades-{m}.zip",
}


@dataclass(frozen=True)
class Job:
    url: str
    dest: Path


def file_url(kind: str, symbol: str, day: date) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; published spot kinds: {sorted(KINDS)}")
    return f"{BASE}/{KINDS[kind].format(sym=symbol, d=day.isoformat())}"


def parse_checksum(text: str) -> str:
    return text.split()[0].strip().lower()


def verify(path: Path, sha256: str) -> bool:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest() == sha256


def days_back(n: int, today: date | None = None) -> list[date]:
    today = today or date.today()
    return [today - timedelta(days=i) for i in range(n, 0, -1)]


def plan(symbols: list[str], kinds: list[str], days: list[date], root: Path) -> list[Job]:
    jobs = []
    for kind in kinds:
        for sym in symbols:
            for d in days:
                url = file_url(kind, sym, d)
                dest = root / kind / sym / url.rsplit("/", 1)[1]
                if not (dest.parent / (dest.name + ".ok")).exists():
                    jobs.append(Job(url, dest))
    return jobs


def month_url(kind: str, symbol: str, ym: str) -> str:
    if kind not in MONTHLY_KINDS:
        raise ValueError(f"unknown monthly kind {kind!r}; known: {sorted(MONTHLY_KINDS)}")
    return f"{MONTHLY_BASE}/{MONTHLY_KINDS[kind].format(sym=symbol, m=ym)}"


def months_span(start_ym: str, today: date | None = None) -> list[str]:
    """Every month from start_ym through the last complete month (monthly files exist only for those)."""
    today = today or date.today()
    y, m = map(int, start_ym.split("-"))
    out = []
    while (y, m) < (today.year, today.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def months_back(n: int, today: date | None = None) -> list[str]:
    today = today or date.today()
    y, m = today.year, today.month
    for _ in range(n):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return months_span(f"{y:04d}-{m:02d}", today)


def plan_monthly(symbols: list[str], kinds: list[str], months: list[str], root: Path) -> list[Job]:
    jobs = []
    for kind in kinds:
        for sym in symbols:
            for ym in months:
                url = month_url(kind, sym, ym)
                dest = root / "monthly" / kind / sym / url.rsplit("/", 1)[1]
                if not (dest.parent / (dest.name + ".ok")).exists():
                    jobs.append(Job(url, dest))
    return jobs


def fetch(job: Job, retries: int = 3) -> str:
    """Return 'ok', 'missing' (404, not published yet) or 'bad:<reason>'."""
    for attempt in range(retries):
        try:
            sha = parse_checksum(urllib.request.urlopen(job.url + ".CHECKSUM", timeout=30).read().decode())
            job.dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = job.dest.with_suffix(".part")
            with urllib.request.urlopen(job.url, timeout=120) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            if not verify(tmp, sha):
                tmp.unlink()
                return "bad:checksum"
            tmp.replace(job.dest)
            (job.dest.parent / (job.dest.name + ".ok")).write_text(sha)
            return "ok"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "missing"
            err = f"http {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            err = type(e).__name__
        time.sleep(2 ** attempt)
    return f"bad:{err}"


def main() -> None:
    from trading.stream.run_ingest import REAL_SYMS
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", default=",".join(s.upper() for s in REAL_SYMS))
    ap.add_argument("--kinds", default=",".join(KINDS))
    ap.add_argument("--out", default="data/local/history")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--monthly", default=None, help="monthly kinds, e.g. klines_1m,klines_1s (replaces daily mode)")
    ap.add_argument("--since", default=None, help="first month YYYY-MM for --monthly (listing gaps return 404)")
    ap.add_argument("--months", type=int, default=None, help="last N full months for --monthly")
    a = ap.parse_args()
    if a.monthly:
        months = months_span(a.since) if a.since else months_back(a.months or 12)
        jobs = plan_monthly(a.symbols.split(","), a.monthly.split(","), months, Path(a.out))
    else:
        jobs = plan(a.symbols.split(","), a.kinds.split(","), days_back(a.days), Path(a.out))
    print(f"{len(jobs)} files to fetch", flush=True)
    results: dict[str, int] = {}
    with ThreadPoolExecutor(a.workers) as pool:
        for i, (job, res) in enumerate(zip(jobs, pool.map(fetch, jobs)), 1):
            results[res.split(":")[0]] = results.get(res.split(":")[0], 0) + 1
            if res != "ok":
                print(f"  {res}: {job.url.rsplit('/', 1)[1]}", flush=True)
            if i % 50 == 0:
                print(f"  {i}/{len(jobs)} {results}", flush=True)
    size = sum(p.stat().st_size for p in Path(a.out).rglob("*.zip"))
    print(f"done: {results}; {size / 2**30:.2f} GiB on disk", flush=True)


if __name__ == "__main__":
    main()
