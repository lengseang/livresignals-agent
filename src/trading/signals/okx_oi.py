"""OKX open-interest HISTORY — the series Binance will not give us.

WHY THIS MODULE EXISTS (found 2026-10-03 by the autonomous improvement agent)
----------------------------------------------------------------------------
`references/external-signals-and-tool-vetting.md` §4 says open interest is the
one genuinely NEW capability found (positioning data, not derivable from
candles) but records its fatal constraint:

    "Honest constraint: Binance exposes only ~30 days of OI history, so it
     cannot be backtested — it can only be monitored forward."

That constraint is FALSE for OKX. The public, UNAUTHENTICATED endpoint

    POST https://www.okx.com/api/v5/aigc/mcp/oi-history
    {"instId": "BTC-USDT-SWAP", "bar": "1D", "limit": 500, "ts": <ms>}

returns `limit` bars, and the `ts` parameter paginates BACKWARD. Measured
2026-10-03: 4 pages of 1D bars reached 2023-12-31 → 2026-10-03 = **1008
gap-free daily rows** (~2.75 years). This converts OI from "MONITOR-ONLY,
cannot be backtested" into a **backtestable** series.

Fields per row: oiCcy, oiCont, oiUsd, oiDeltaUsd, oiDeltaPct, ts.

⚠ CAVEAT — this is OKX's OI, not Binance's. For a veto applied to Binance
spot/futures we use OKX BTC-USDT-SWAP OI as a MARKET-WIDE positioning proxy.
It is a different venue's book, so it is a proxy, not the exact series our
orders sit in. State that plainly whenever a result from this data is quoted.

No credentials, no npm, no Node — plain HTTPS POST via urllib. VPS-safe.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Iterable

_URL = "https://www.okx.com/api/v5/aigc/mcp/oi-history"
_H = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
_BARS = {"5m", "15m", "1H", "4H", "1D"}


def _post(body: dict, timeout: int = 25) -> dict:
    req = urllib.request.Request(
        _URL, data=json.dumps(body).encode(), method="POST", headers=_H)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_page(inst_id: str, bar: str = "1D", limit: int = 500,
               ts: int | None = None) -> list[dict]:
    """One page of OI bars, oldest→newest. `ts` returns bars with ts <= value.

    Raises on network/parse error (caller decides whether to fail closed).
    Returns [] when the venue simply has no rows for the instrument.
    """
    if bar not in _BARS:
        raise ValueError(f"bar must be one of {sorted(_BARS)}")
    body: dict = {"instId": inst_id, "bar": bar, "limit": max(1, min(500, limit))}
    if ts is not None:
        body["ts"] = int(ts)
    d = _post(body)
    data = d.get("data") or []
    if not data:
        return []
    return data[0].get("rows") or []


def fetch_history(inst_id: str, bar: str = "1D", pages: int = 6,
                  pause: float = 0.2) -> list[dict]:
    """Paginate backward via `ts` and return the FULL de-duplicated series.

    Ascending by ts. `pages` caps how far back we walk (each page <= 500 bars).
    A short/empty page ends the walk early.
    """
    seen: dict[int, dict] = {}
    ts: int | None = None
    for _ in range(max(1, pages)):
        rows = fetch_page(inst_id, bar=bar, limit=500, ts=ts)
        if not rows:
            break
        for r in rows:
            seen[int(r["ts"])] = r
        oldest = min(int(r["ts"]) for r in rows)
        if ts is not None and oldest >= ts:      # no progress → stop
            break
        prev_ts = ts
        ts = oldest - 1
        # NOTE: the *unpaginated* first call returns only the current ~31-bar
        # window regardless of limit=500 (measured 2026-10-03). Do NOT stop on a
        # short page — the short page is the current window, and `ts` pagination
        # returns full 500-bar pages behind it. Only stop when a page yields no
        # NEW rows.
        if prev_ts is not None and not any(
                int(r["ts"]) < prev_ts for r in rows):
            break
        time.sleep(pause)                        # polite
    return [seen[k] for k in sorted(seen)]


def to_daily_series(rows: Iterable[dict]) -> tuple[list[str], list[float]]:
    """(dates, oi_usd) from raw rows, ascending, de-duplicated by date.

    Dates are UTC yyyy-mm-dd. Pure → unit-testable without network.
    """
    import datetime as _dt
    by_date: dict[str, float] = {}
    for r in rows:
        d = _dt.datetime.fromtimestamp(int(r["ts"]) / 1000, _dt.timezone.utc)
        by_date[d.strftime("%Y-%m-%d")] = float(r["oiUsd"])
    keys = sorted(by_date)
    return keys, [by_date[k] for k in keys]


def oi_change_pct(oi_usd: list[float], idx: int, lookback: int = 3) -> float | None:
    """Percent change in OI over `lookback` bars ending at idx. None if too short.

    Pure function. Used by the veto rule: skip entries where price is RISING
    while OI is FALLING (short-covering squeeze, prone to fading) — the intended
    use recorded in §4 of the tool-vetting reference.
    """
    if idx - lookback < 0 or idx >= len(oi_usd) or oi_usd[idx - lookback] == 0:
        return None
    return (oi_usd[idx] / oi_usd[idx - lookback] - 1.0) * 100.0
