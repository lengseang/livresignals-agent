"""CTREND-lite (candidate A2). REPLAY/PAPER only.

Re-implementation of Fieberg, Liedtke, Poddig, Walker & Zaremba (2025), "A Trend Factor for the Cross Section of
Cryptocurrency Returns", JFQA 60 (open access, CC-BY), sections III.B-C, rules recorded in
research/candidates/candidates.json (A2) before any run:
  weekly (Monday 00:00 UTC), per coin, 28 technical indicators from daily bars;
  each indicator -> cross-sectional rank mapped to [-0.5, 0.5];
  per indicator, a univariate cross-sectional regression of next-week return on the indicator, coefficients
  averaged over the last 52 weeks (Fama-MacBeth) -> one forecast per indicator;
  elastic net (L1/L2 mix 0.5, strength by corrected AIC, coefficients > 0) over the last 52 weeks of those
  forecasts selects the informative ones; CTREND = mean of the selected forecasts;
  hold the top quintile by CTREND.
Declared deviations (candidates.json A2): 15-38 Binance coins instead of ~3,000; long-only top quintile;
equal weights everywhere (no market-cap history); weights drift inside the week (the broker has no partial
rebalance); cci and Chaikin periods 20 (Appendix A not read); returns not in excess of a risk-free rate;
if the elastic net selects nothing, the plain average of all 28 forecasts is used (paper's eq. 9).
"""
from __future__ import annotations

import math

import numpy as np

from trading.replay.bars import INTERVAL_MS, Bar
from trading.replay.strategy import Signal

D = INTERVAL_MS["1d"]
W = 7 * D
MONDAY = 4 * D                      # 1970-01-01 was a Thursday; +4 days = Monday 00:00 UTC
N_IND = 28
MIN_BARS = 201                      # SMA200 of close and of dollar volume


# ----- indicators -------------------------------------------------------------------------------------------
def _sma(x: np.ndarray, n: int) -> float:
    return float(x[-n:].mean())


def _ema_series(x: np.ndarray, n: int) -> np.ndarray:
    a, out = 2 / (n + 1), np.empty(len(x))
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _rsi_series(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(c)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    out = np.full(len(c), np.nan)
    for i in range(n, len(c)):
        g, l = up[i - n:i].mean(), dn[i - n:i].mean()
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def indicators(h: np.ndarray, l: np.ndarray, c: np.ndarray, qv: np.ndarray) -> np.ndarray | None:
    """The paper's 28 signals from daily high/low/close/dollar volume (oldest first). None if too short."""
    if len(c) < MIN_BARS or c[-1] <= 0 or qv[-1] <= 0:
        return None
    out = []
    rsi = _rsi_series(c)
    out.append(rsi[-1])                                                     # rsi
    k = np.array([(c[i] - l[i - 13:i + 1].min()) / max(h[i - 13:i + 1].max() - l[i - 13:i + 1].min(), 1e-12)
                  for i in range(len(c) - 3, len(c))]) * 100
    out += [k[-1], k.mean()]                                                # stochK, stochD
    r14 = rsi[-14:]
    out.append((rsi[-1] - r14.min()) / max(r14.max() - r14.min(), 1e-12))  # stochRSI
    tp = (h + l + c) / 3
    md = np.abs(tp[-20:] - tp[-20:].mean()).mean()
    out.append((tp[-1] - tp[-20:].mean()) / max(0.015 * md, 1e-12))        # cci (20)
    out += [_sma(c, n) / c[-1] for n in (3, 5, 10, 20, 50, 100, 200)]       # sma_n scaled by close
    macd = (_ema_series(c, 26) - _ema_series(c, 12)) / _ema_series(c, 12)  # slow - fast, % of fast (paper)
    out += [macd[-1], macd[-1] - _ema_series(macd, 9)[-1]]                  # macd, macd_diff_signal
    out += [_sma(qv, n) / qv[-1] for n in (3, 5, 10, 20, 50, 100, 200)]     # volsma_n / current volume
    vm = (_ema_series(qv, 26) - _ema_series(qv, 12)) / _ema_series(qv, 12)
    out += [vm[-1], vm[-1] - _ema_series(vm, 9)[-1]]                        # volmacd, volmacd_diff_signal
    rng = np.maximum(h[-20:] - l[-20:], 1e-12)
    mfv = ((c[-20:] - l[-20:]) - (h[-20:] - c[-20:])) / rng * qv[-20:]
    out.append(mfv.sum() / qv[-20:].sum())                                  # chaikin money flow (20)
    mid, sd = c[-20:].mean(), c[-20:].std(ddof=0)
    out += [(mid - 2 * sd) / c[-1], mid / c[-1], (mid + 2 * sd) / c[-1], 4 * sd / mid]   # boll low/mid/high/width
    v = np.array(out, dtype=float)
    return v if len(v) == N_IND and np.all(np.isfinite(v)) else None


def rank_half(x: np.ndarray) -> np.ndarray:
    """Cross-sectional ranks of each column mapped to [-0.5, 0.5] (ties get the mean rank)."""
    n = x.shape[0]
    if n == 1:
        return np.zeros_like(x)
    out = np.empty_like(x, dtype=float)
    for j in range(x.shape[1]):
        order = x[:, j].argsort(kind="mergesort")
        r = np.empty(n)
        r[order] = np.arange(n)
        for val in np.unique(x[:, j]):                                     # average ties
            m = x[:, j] == val
            r[m] = r[m].mean()
        out[:, j] = r / (n - 1) - 0.5
    return out


# ----- elastic-net selection ------------------------------------------------------------------------------
def select_forecasts(F: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Indices j with a positive elastic-net weight (l1_ratio 0.5, alpha by AICc). F: rows x J forecasts."""
    from sklearn.linear_model import enet_path
    n = len(r)
    if n < 10:
        return np.array([], dtype=int)
    Fc, rc = F - F.mean(axis=0), r - r.mean()                               # intercept handled by centring
    alphas, coefs, _ = enet_path(Fc, rc, l1_ratio=0.5, positive=True, alphas=100)
    best, best_aicc = None, math.inf
    for i in range(len(alphas)):
        beta = coefs[:, i]
        rss = float(((rc - Fc @ beta) ** 2).sum())
        k = int((beta > 0).sum()) + 1                                       # + intercept
        if rss <= 0 or n - k - 1 <= 0:
            continue
        aicc = n * math.log(rss / n) + 2 * k + 2 * k * (k + 1) / (n - k - 1)
        if aicc < best_aicc:
            best, best_aicc = beta, aicc
    return np.array([], dtype=int) if best is None else np.flatnonzero(best > 0)


# ----- the strategy ---------------------------------------------------------------------------------------
class CTrendLite:
    intervals = ("1d",)
    caveats = ("CTREND-lite differs from the paper (JFQA 2025): 15-38 Binance coins not ~3,000, long-only top "
               "quintile, equal weights (no market-cap history), weights drift within the week, cci/Chaikin "
               "periods 20 (Appendix A not read), no risk-free rate. See research/candidates/candidates.json A2.",)

    def __init__(self, window_weeks: int = 52, quantile: int = 5, btc: str = "BTCUSDT"):
        self.name = "ctrend_lite"
        self.params = {"window_weeks": window_weeks, "quantile": quantile, "btc": btc, "interval": "1d",
                       "source": "Fieberg et al. 2025 JFQA", "candidate": "A2-ctrend-lite"}
        self._m, self._q, self._btc = window_weeks, quantile, btc
        self._seen: list[str] = []
        self._weeks: list[int] = []                       # week-end instants processed, oldest first
        self._z: dict[int, dict[str, np.ndarray]] = {}    # week -> symbol -> ranked indicators
        self._close: dict[int, dict[str, float]] = {}     # week -> symbol -> close at the week end
        self._coef: dict[int, np.ndarray] = {}            # week k -> (J, 2) univariate [a, b] for pair k
        self._fc: dict[int, dict[str, np.ndarray]] = {}   # week s -> symbol -> J univariate forecasts
        self.last_ctrend: dict[str, float] = {}           # latest scores (inspection / terminal)

    # -- panel bookkeeping --
    def _ret(self, k: int, sym: str) -> float | None:
        """Return over week k -> k+1 for sym (both closes needed)."""
        i = self._weeks.index(k)
        if i + 1 >= len(self._weeks):
            return None
        a, b = self._close[k].get(sym), self._close[self._weeks[i + 1]].get(sym)
        return None if a is None or b is None or a <= 0 else b / a - 1

    def _fit_pair(self, k: int) -> None:
        """Univariate cross-sectional OLS of r(k->k+1) on each ranked indicator at k."""
        syms = [s for s in self._z[k] if self._ret(k, s) is not None]
        if len(syms) < 3:
            return
        Z = np.array([self._z[k][s] for s in syms])
        r = np.array([self._ret(k, s) for s in syms])
        c = np.zeros((N_IND, 2))
        for j in range(N_IND):
            x = Z[:, j]
            vx = ((x - x.mean()) ** 2).sum()
            b = 0.0 if vx == 0 else float(((x - x.mean()) * (r - r.mean())).sum() / vx)
            c[j] = (r.mean() - b * x.mean(), b)
        self._coef[k] = c

    def _forecasts_at(self, s: int) -> dict[str, np.ndarray] | None:
        """Univariate forecasts for week s from the last M fitted pairs whose returns were known at s."""
        i = self._weeks.index(s)
        ks = [k for k in self._weeks[max(0, i - self._m):i] if k in self._coef]
        if len(ks) < self._m:
            return None
        avg = np.mean([self._coef[k] for k in ks], axis=0)                 # (J, 2)
        return {sym: avg[:, 0] + avg[:, 1] * z for sym, z in self._z[s].items()}

    def _ctrend(self, t: int) -> dict[str, float] | None:
        fc_t = self._fc.get(t)
        if fc_t is None:
            return None
        i = self._weeks.index(t)
        rows, ys = [], []
        for s in self._weeks[max(0, i - self._m):i]:
            if s not in self._fc:
                continue
            for sym, f in self._fc[s].items():
                r = self._ret(s, sym)
                if r is not None:
                    rows.append(f)
                    ys.append(r)
        if len({s for s in self._weeks[max(0, i - self._m):i] if s in self._fc}) < self._m:
            return None                                                     # a full 52-week training window
        F, y = np.array(rows), np.array(ys)
        lo, hi = np.percentile(y, [0.5, 99.5])                              # paper: truncate extreme returns
        keep = (y >= lo) & (y <= hi)
        sel = select_forecasts(F[keep], y[keep])
        use = sel if len(sel) else np.arange(N_IND)                         # none selected -> eq. 9 average
        return {sym: float(f[use].mean()) for sym, f in fc_t.items()}

    # -- engine hook --
    def on_bar(self, bar: Bar, ctx) -> list[Signal]:
        if bar.symbol not in self._seen:
            self._seen.append(bar.symbol)
        if bar.symbol != self._btc or (bar.end_ms - MONDAY) % W != 0:
            return []
        t = bar.end_ms
        feats, closes = {}, {}
        for sym in sorted(self._seen):
            hist = ctx.history(sym, "1d", MIN_BARS + 60)
            if not hist or hist[-1].end_ms != t:
                continue
            c = np.array([b.close for b in hist])
            v = indicators(np.array([b.high for b in hist]), np.array([b.low for b in hist]), c,
                           np.array([b.quote_volume for b in hist]))
            closes[sym] = float(c[-1])
            if v is not None:
                feats[sym] = v
        self._weeks.append(t)
        self._close[t] = closes
        syms = sorted(feats)
        self._z[t] = dict(zip(syms, rank_half(np.array([feats[s] for s in syms])))) if len(syms) >= 3 else {}
        if len(self._weeks) >= 2:
            self._fit_pair(self._weeks[-2])                                 # its return just became known
        fc = self._forecasts_at(t)
        if fc is not None:
            self._fc[t] = fc
        scores = self._ctrend(t)
        if not scores:
            return []
        self.last_ctrend = scores
        k = max(1, len(scores) // self._q)
        top = [s for s, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]
        now, out = ctx.now_ms, []
        for sym in sorted(self._seen):
            if ctx.position(sym) is not None and sym not in top:
                out.append(Signal(self.name, sym, "close", now, "left the top CTREND quintile",
                                  closes.get(sym, bar.close), "1d"))
        for sym in top:
            if ctx.position(sym) is None:
                out.append(Signal(self.name, sym, "open", now, f"top CTREND quintile ({scores[sym]:+.5f})",
                                  closes[sym], "1d"))
        return out
