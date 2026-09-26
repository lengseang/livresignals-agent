"""Backfill public daily market data from data.binance.vision (paper research only).

Every file is checked against Binance's published SHA-256 before it is kept; a
file that fails is deleted, never used. Spot bookTicker is not published
historically, so spreads come only from the live recorder (run_ingest).

    python -m trading.stream.history --days 30
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
    a = ap.parse_args()
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
