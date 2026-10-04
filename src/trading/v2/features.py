"""Feature engine (Contract B): deterministic, computed from CLOSED bars, never fetched.

FEATURE_VERSION "features-v1" definition (changing any of this MUST bump the version, because a changed
definition silently invalidates every backtest that used the old one):
  returns[h]      close / close h minutes ago - 1, from 1m bars (h = 1, 5, 15, 60, 240)
  rsi             Wilder RSI(14) on the last N_1H=120 1h closes (Wilder smoothing is path-dependent, so the
                  window length is part of the definition)
  atr             Wilder ATR(14) on 1h bars / last 1h close (a fraction of price)
  ema_fast_slope  EMA20 (1h) last-bar fractional change;   ema_slow_slope: same for EMA50
  distance_vwap   last 1m close / VWAP of the last 60 1m bars - 1   (VWAP = sum quote vol / sum base vol)
  volume_1m       base volume of the last closed 1m bar
  volume_zscore   (volume_1m - mean) / sample std of the PREVIOUS 60 1m volumes
  orderbook       top-of-book only: bid_depth/ask_depth = best bid/ask quantity; imbalance = (b-a)/(b+a)
  regime.trend    existing classifier (`signals.regime`): TRENDING -> bullish if EMA20 > EMA50 else bearish;
                  otherwise neutral
  regime.volatility "high" if the classifier says VOLATILE else "normal" (the spec's TRENDING > VOLATILE
                  precedence means a trending-and-volatile market reads "normal": documented limitation)
  regime.liquidity "thin" if spread_bps > LIQUIDITY_THIN_SPREAD_BPS else "normal". UNVALIDATED constant.

No synthetic numbers: if ANY input is missing or too short, NO snapshot is produced and the result names
exactly what is missing. A feature is never zero-filled. No look-ahead: bars that end after `as_of`
(the last closed 1m bar) are ignored. `now_ms` is injected, so output is deterministic.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from trading.replay.bars import Bar
from trading.signals.indicators import atr, ema
from trading.signals.regime import classify_regime
from trading.v2.contracts import FeatureSnapshot
from trading.v2.events import iso_ms
from trading.v2.ids import derive_id

FEATURE_VERSION = "features-v1"
RETURN_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
N_1M = max(RETURN_MINUTES.values()) + 1          # 241 one-minute bars
N_1H = 120                                       # hourly bars requested (regime needs >= 60, EMA50 >= 51)
MIN_1H = 60
VOL_WINDOW = 60
VWAP_WINDOW = 60
RSI_PERIOD = 14
LIQUIDITY_THIN_SPREAD_BPS = 5.0                  # UNVALIDATED: equals the spec's example spread cap
MAX_BOOK_AGE_MS = 5_000


class BarSource(Protocol):
    def tail(self, symbol: str, interval: str, n: int) -> Sequence[Bar]: ...


@dataclass(frozen=True)
class Book:
    ts_ms: int
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float


class BookSource(Protocol):
    def latest(self, symbol: str) -> Optional[Book]: ...


@dataclass(frozen=True)
class FeatureResult:
    snapshot: Optional[FeatureSnapshot]
    missing: tuple[str, ...]      # empty iff snapshot is not None
    spread_bps: Optional[float]
    last_close: Optional[float]

    @property
    def ok(self) -> bool:
        return self.snapshot is not None


def snapshot_content_id(snapshot_dict: dict) -> str:
    """The id of a snapshot, DERIVED FROM ITS CONTENT: symbol, as-of, definition version and a hash of every
    value. Two snapshots with the same id have the same numbers, so DecisionState.state_hash() (which includes
    the id) pins exactly what the decision saw. `snapshot_dict` is a snapshot's dict form; its own id is ignored."""
    body = {k: v for k, v in snapshot_dict.items() if k != "snapshot_id"}
    h = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    return derive_id("feat", snapshot_dict["symbol"], snapshot_dict["as_of_utc"],
                     snapshot_dict["feature_version"], h)


def verify_snapshot(snapshot: FeatureSnapshot) -> bool:
    """True iff the snapshot's id matches its own content (it has not been edited or mislabelled)."""
    return snapshot.snapshot_id == snapshot_content_id(snapshot.to_dict())


def rsi_wilder(closes: Sequence[float], period: int = RSI_PERIOD) -> Optional[float]:
    """Wilder RSI of the LAST close. None if there are too few closes or the series is perfectly flat
    (0/0 is undefined; it is reported as missing, not as 50)."""
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
    if ag == 0 and al == 0:
        return None
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _rows(bars: Sequence[Bar]) -> list[dict]:
    return [{"open": b.open, "high": b.high, "low": b.low, "close": b.close} for b in bars]


OVERFETCH = 50   # extra bars requested so filtering never shortens the window below what was asked


def _clean(bars: Sequence[Bar], symbol: str, interval: str, as_of_ms: Optional[int], n: int) -> list[Bar]:
    """Keep only this symbol/interval, drop bars that end after `as_of_ms` (no look-ahead), THEN take the
    last n: a foreign or future bar must never displace a valid one from the window."""
    out = [b for b in bars if b.symbol == symbol and b.interval == interval
           and (as_of_ms is None or b.end_ms <= as_of_ms)]
    for a, b in zip(out, out[1:]):
        if b.end_ms <= a.end_ms:
            raise ValueError(f"{symbol} {interval}: bars not strictly increasing at {b.end_ms}")
    return out[-n:]


def _gap_at(bars: Sequence[Bar], step_ms: int) -> Optional[int]:
    """end_ms of the first bar that does not follow its predecessor by exactly one period, else None."""
    for a, b in zip(bars, bars[1:]):
        if b.end_ms - a.end_ms != step_ms:
            return b.end_ms
    return None


def compute_features(symbol: str, bars: BarSource, book: Optional[BookSource], now_ms: int,
                     *, max_book_age_ms: int = MAX_BOOK_AGE_MS, book_lead_tolerance_ms: int = 0) -> FeatureResult:
    """`book_lead_tolerance_ms`: how far AFTER `now_ms` a book's timestamp may be. 0 (strict) for replays, where a book
    from the future is look-ahead. A live feed fetches the book after `now_ms` was taken, so the live runner allows the
    fetch latency (a few seconds); a book further in the future than that is still rejected."""
    missing: list[str] = []
    m1 = _clean(bars.tail(symbol, "1m", N_1M + OVERFETCH), symbol, "1m", None, N_1M)
    if not m1:
        return FeatureResult(None, ("bars.1m(none)",), None, None)
    as_of_ms = m1[-1].end_ms
    h1 = _clean(bars.tail(symbol, "1h", N_1H + OVERFETCH), symbol, "1h", as_of_ms, N_1H)
    last_close = m1[-1].close

    # bars are indexed by position ("5 bars ago"), so a hole in the series would silently shift every return:
    # a gap is MISSING data, never a wrong number
    gap1 = _gap_at(m1, 60_000)
    if gap1 is not None:
        missing.append(f"bars.1m(gap before {gap1})")
    gap_h = _gap_at(h1, 3_600_000)
    if gap_h is not None:
        missing.append(f"bars.1h(gap before {gap_h})")
        h1 = []

    # --- returns
    returns: dict[str, float] = {}
    if gap1 is not None:
        pass
    elif len(m1) < N_1M:
        missing.append(f"returns(need {N_1M} 1m bars, have {len(m1)})")
    else:
        c = [b.close for b in m1]
        if min(c) <= 0:
            missing.append("returns(non-positive close)")
        else:
            returns = {k: c[-1] / c[-1 - n] - 1.0 for k, n in RETURN_MINUTES.items()}

    # --- 1m volume features
    volume_1m = zscore = vwap_dist = None
    if gap1 is not None:
        pass
    elif len(m1) < VOL_WINDOW + 1:
        missing.append(f"volume(need {VOL_WINDOW + 1} 1m bars, have {len(m1)})")
    else:
        vols = [b.volume for b in m1]
        base = vols[-VOL_WINDOW - 1:-1]
        mean = sum(base) / len(base)
        var = sum((v - mean) ** 2 for v in base) / (len(base) - 1)
        std = math.sqrt(var)
        volume_1m = vols[-1]
        if std > 0:
            zscore = (volume_1m - mean) / std
        else:
            missing.append("volume_zscore(zero variance)")
        w = m1[-VWAP_WINDOW:]
        vsum = sum(b.volume for b in w)
        if vsum > 0:
            vwap = sum(b.quote_volume for b in w) / vsum
            vwap_dist = last_close / vwap - 1.0
        else:
            missing.append("distance_vwap(zero volume)")

    # --- 1h technical + regime
    rsi = atr_rel = slope_f = slope_s = None
    trend = vol_regime = None
    if gap_h is not None:
        pass
    elif len(h1) < MIN_1H:
        missing.append(f"technical/regime(need {MIN_1H} 1h bars, have {len(h1)})")
    else:
        closes = [b.close for b in h1]
        rows = _rows(h1)
        rsi = rsi_wilder(closes)
        if rsi is None:
            missing.append("rsi(undefined)")
        a = atr(rows, 14)
        if a and closes[-1] > 0:
            atr_rel = a[-1] / closes[-1]
        else:
            missing.append("atr")
        e20, e50 = ema(closes, 20), ema(closes, 50)
        if len(e20) >= 2 and len(e50) >= 2 and e20[-2] > 0 and e50[-2] > 0:
            slope_f, slope_s = e20[-1] / e20[-2] - 1.0, e50[-1] / e50[-2] - 1.0
            label = classify_regime(rows)      # >= 60 rows guaranteed: no silent RANGING default
            trend = ("bullish" if e20[-1] > e50[-1] else "bearish") if label == "TRENDING" else "neutral"
            vol_regime = "high" if label == "VOLATILE" else "normal"
        else:
            missing.append("ema_slopes/regime")

    # --- top of book
    spread_bps = imbalance = bid_q = ask_q = None
    bk = book.latest(symbol) if book is not None else None
    if bk is None:
        missing.append("orderbook(none)")
    elif bk.ts_ms > now_ms + book_lead_tolerance_ms:
        missing.append("orderbook(timestamp after now: look-ahead)")
    elif now_ms - bk.ts_ms > max_book_age_ms:
        missing.append(f"orderbook(stale {now_ms - bk.ts_ms}ms)")
    elif not (0 < bk.bid <= bk.ask) or bk.bid_qty < 0 or bk.ask_qty < 0 or (bk.bid_qty + bk.ask_qty) <= 0:
        missing.append("orderbook(invalid)")
    else:
        mid = (bk.bid + bk.ask) / 2
        spread_bps = (bk.ask - bk.bid) / mid * 1e4
        imbalance = (bk.bid_qty - bk.ask_qty) / (bk.bid_qty + bk.ask_qty)
        bid_q, ask_q = bk.bid_qty, bk.ask_qty

    if missing:
        return FeatureResult(None, tuple(missing), spread_bps, last_close)

    liquidity = "thin" if spread_bps > LIQUIDITY_THIN_SPREAD_BPS else "normal"
    values = dict(
        contract="feature_snapshot", version="1.0", snapshot_id="feat_PLACEHOLDER",
        feature_version=FEATURE_VERSION, symbol=symbol, as_of_utc=iso_ms(as_of_ms),
        returns=returns,
        technical={"rsi": rsi, "atr": atr_rel, "ema_fast_slope": slope_f,
                   "ema_slow_slope": slope_s, "distance_vwap": vwap_dist},
        volume={"volume_1m": volume_1m, "volume_zscore": zscore},
        orderbook={"imbalance": imbalance, "bid_depth": bid_q, "ask_depth": ask_q},
        regime={"trend": trend, "volatility": vol_regime, "liquidity": liquidity},
        data_quality={"complete": True, "missing_fields": [], "stale_ms": max(0, now_ms - as_of_ms)},
    )
    values["snapshot_id"] = snapshot_content_id(FeatureSnapshot(**values).to_dict())
    snap = FeatureSnapshot(**values)
    return FeatureResult(snap, (), spread_bps, last_close)
