"""Multi-day context features ("context-v1"): what features-v1 cannot see. Used IDENTICALLY by the live pipeline and
by the study that fitted and evaluated them (`studies/context_fit.py`), so the numbers evaluated are the numbers used.

Why (docs/v2/evidence/entry_study_v2.json, pre-registered, 15 coins, walk-forward 2023-2026): a model on multi-day
features had out-of-sample probability skill every year (AUC 0.60 / 0.55 / 0.54 / 0.53) where features-v1 had none
(0.49). Its TRADING edge did not clear the strict bar, so this is evidence for better-informed answers, not for
trading more.

Definition (changing anything MUST bump CONTEXT_VERSION). Input: closed 1h bars, the last N_CTX_1H of them, with no
gap. i = the last closed bar.
  z{k}d          (close[i]/close[i-24k] - 1) / (dvol * sqrt(k)), k = 1, 3, 7, 14, 30; dvol = std of the last 720
                 hourly log returns x sqrt(24)
  sma20_gap      close / SMA(480 h) - 1;  sma50_gap: close / SMA(1200 h) - 1;  sma20_slope: SMA480 now / 24 h ago - 1
  donchian_pos   (close - min low) / (max high - min low) over the 480 bars BEFORE i;  breakout: close > that max high
  rsi_d, atr_d   Wilder RSI(14) and ATR(14)/close on the complete UTC days inside the window
  vol_ratio      std of the last 168 hourly log returns / std of the last 720
  qv_ratio       mean quote volume of the last 168 bars / mean of the last 720
  ret7, ret30    plain 7-day / 30-day returns (inputs to the cross-section)
Cross-section (needs every universe coin at the same instant): xs_rank7 / xs_rank30 = position of the coin's 7d / 30d
return among the coins available (0 = worst, 1 = best); rel_btc7 = ret7 - BTC ret7; btc_ret30; btc_above_sma50.
"""
from __future__ import annotations

import math
from typing import Mapping, Optional, Sequence

import numpy as np

from trading.replay.bars import Bar

CONTEXT_VERSION = "context-v1"
N_CTX_1H = 1250
H = 3_600_000
DAY = 24 * H
CONTEXT_FIELDS = ("z1d", "z3d", "z7d", "z14d", "z30d", "sma20_gap", "sma50_gap", "sma20_slope", "donchian_pos",
                  "rsi_d", "atr_d", "vol_ratio", "xs_rank7", "xs_rank30", "rel_btc7", "btc_ret30", "btc_above_sma50",
                  "qv_ratio")
UNIVERSE = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT", "LINKUSDT", "DOGEUSDT", "AVAXUSDT",
            "NEARUSDT", "SUIUSDT", "ARBUSDT", "DOTUSDT", "LTCUSDT", "ATOMUSDT")

PRESETS = {"top3": UNIVERSE[:3], "top8": UNIVERSE[:8], "all15": UNIVERSE}      # the rollout steps (3 -> 8 -> 15)


def resolve_symbols(text: str) -> tuple:
    """`--symbols` / V2_SYMBOLS: comma separated pairs and/or the presets top3 | top8 | all15. De-duplicated, order kept,
    each pair validated (A-Z0-9, 5-20 chars). Raises ValueError on anything else."""
    import re
    out: list = []
    for part in (p.strip() for p in text.split(",")):
        if not part:
            continue
        items = PRESETS.get(part.lower()) or (part.upper(),)
        for sym in items:
            if not re.fullmatch(r"[A-Z0-9]{5,20}", sym):
                raise ValueError(f"not a trading pair: {sym!r}")
            if sym not in out:
                out.append(sym)
    if not out:
        raise ValueError("no symbols")
    return tuple(out)


def _wilder(x: np.ndarray, n: int) -> float:
    if len(x) <= n:
        return float("nan")
    a = float(x[1:n + 1].mean())
    for v in x[n + 1:]:
        a = (a * (n - 1) + float(v)) / n
    return a


def coin_row(t: np.ndarray, o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, qv: np.ndarray,
             i: int) -> Optional[dict]:
    """The per-coin part, from bars [i - N_CTX_1H + 1, i] only. None if the window is short or has a gap."""
    s = i - N_CTX_1H + 1
    if s < 0 or int(t[i] - t[s]) != (N_CTX_1H - 1) * H:
        return None
    cw, hw, lw, tw, qw = c[s:i + 1], h[s:i + 1], l[s:i + 1], t[s:i + 1], qv[s:i + 1]
    lr = np.diff(np.log(cw))
    dvol = float(lr[-720:].std()) * math.sqrt(24)
    v7, v30 = float(lr[-168:].std()), float(lr[-720:].std())
    if not (dvol > 0 and v30 > 0):
        return None
    last = cw[-1]
    z = {f"z{k}d": (last / cw[-1 - 24 * k] - 1) / (dvol * math.sqrt(k)) for k in (1, 3, 7, 14, 30)}
    sma20, sma50 = float(cw[-480:].mean()), float(cw[-1200:].mean())
    sma20_prev = float(cw[-504:-24].mean())
    hh, ll = float(hw[-481:-1].max()), float(lw[-481:-1].min())
    # complete UTC days inside the window (24 bars, the day fully closed by the end of bar i)
    day = tw // DAY
    starts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
    ends = np.r_[starts[1:], len(day)]
    full = [(a, b) for a, b in zip(starts, ends) if b - a == 24]
    if len(full) < 30:
        return None
    dc = np.array([cw[b - 1] for a, b in full]); dh = np.array([hw[a:b].max() for a, b in full])
    dl = np.array([lw[a:b].min() for a, b in full])
    tr = np.r_[dh[0] - dl[0], np.maximum(dh[1:] - dl[1:], np.maximum(abs(dh[1:] - dc[:-1]), abs(dl[1:] - dc[:-1])))]
    ch = np.r_[0.0, np.diff(dc)]
    up, dn = _wilder(np.maximum(ch, 0), 14), _wilder(np.maximum(-ch, 0), 14)
    rsi = 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)
    qv7, qv30 = float(qw[-168:].mean()), float(qw[-720:].mean())
    return {**z, "sma20_gap": last / sma20 - 1, "sma50_gap": last / sma50 - 1, "sma20_slope": sma20 / sma20_prev - 1,
            "donchian_pos": (last - ll) / (hh - ll) if hh > ll else 0.5, "breakout": float(last > hh),
            "rsi_d": float(rsi), "atr_d": _wilder(tr, 14) / float(dc[-1]), "vol_ratio": v7 / v30,
            "qv_ratio": qv7 / qv30 if qv30 > 0 else 1.0,
            "ret7": last / cw[-1 - 168] - 1, "ret30": last / cw[-1 - 720] - 1}


def cross_section(rows: Mapping[str, dict], symbol: str, btc_row: dict, btc_above: float) -> dict:
    """The cross-sectional part. `rows` = every coin with a row at this instant (must include `symbol`)."""
    def rank(key):
        vals = sorted(r[key] for r in rows.values())
        return vals.index(rows[symbol][key]) / (len(vals) - 1) if len(vals) > 1 else 0.5
    r = rows[symbol]
    return {"xs_rank7": rank("ret7"), "xs_rank30": rank("ret30"), "rel_btc7": r["ret7"] - btc_row["ret7"],
            "btc_ret30": btc_row["ret30"], "btc_above_sma50": btc_above}


def vector(full: dict) -> list[float]:
    return [float(full[f]) for f in CONTEXT_FIELDS]


def _arrays(bars: Sequence[Bar]):
    a = np.array([(b.open_ms, b.open, b.high, b.low, b.close, b.quote_volume) for b in bars], dtype=float)
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]


def live_context(bars_1h: Mapping[str, Sequence[Bar]], symbol: str, as_of_ms: int) -> tuple[Optional[dict], tuple]:
    """The context for `symbol` at `as_of_ms` from closed 1h bars of the universe. Returns (context or None, missing).
    Only bars that ENDED at or before as_of_ms are used (no look-ahead). BTC must be available (it anchors the regime);
    at least 3 coins are needed for a rank."""
    rows, missing = {}, []
    for s, bars in bars_1h.items():
        ok = [b for b in bars if b.end_ms <= as_of_ms and b.interval == "1h" and b.symbol == s]
        if len(ok) < N_CTX_1H:
            missing.append(f"context.{s}(have {len(ok)} 1h bars, need {N_CTX_1H})")
            continue
        t, o, h, l, c, qv = _arrays(ok[-N_CTX_1H:])
        if int(as_of_ms - (t[-1] + H)) >= H:            # the last closed hour must be the one ending by now
            missing.append(f"context.{s}(stale)")
            continue
        r = coin_row(t, o, h, l, c, qv, len(t) - 1)
        if r is None:
            missing.append(f"context.{s}(gap or short window)")
            continue
        r["_sma50"] = float(c[-1200:].mean()); r["_close"] = float(c[-1])
        rows[s] = r
    if symbol not in rows or "BTCUSDT" not in rows or len(rows) < 3:
        return None, tuple(missing) or ("context(universe incomplete)",)
    btc = rows["BTCUSDT"]
    xs = cross_section(rows, symbol, btc, 1.0 if btc["_close"] > btc["_sma50"] else 0.0)
    full = {**rows[symbol], **xs}
    out = {f: float(full[f]) for f in CONTEXT_FIELDS}
    out["n_universe"] = len(rows)
    out["context_version"] = CONTEXT_VERSION
    return out, tuple(missing)
