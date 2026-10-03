# OKX open-interest research (found 2026-10-03, overnight shift)

## What this is

`references/external-signals-and-tool-vetting.md` §4 records open interest as the
one genuinely NEW capability (positioning data, not derivable from candles) but
with a fatal constraint:

> "Binance exposes only ~30 days of OI history, so it cannot be backtested — it
>  can only be monitored forward."

**That constraint is venue-specific and now broken.** OKX exposes a public,
UNAUTHENTICATED OI history endpoint that paginates backward via `ts`:

```
POST https://www.okx.com/api/v5/aigc/mcp/oi-history
{"instId":"BTC-USDT-SWAP","bar":"1D","limit":500,"ts":<ms>}
```

Measured 2026-10-03: 1008 gap-free daily bars, 2023-12-31 → 2026-10-03 (~2.75y).
Fetcher: `src/trading/signals/okx_oi.py`. Snapshot: `okx_oi_daily.json`.

⚠ It is **OKX's** OI, used as a market-wide positioning proxy for our Binance
book. Different venue — say so whenever quoting a result.

## The veto rule tested

> Skip an entry when BTC **price rose** over LOOKBACK days while BTC **OI fell**
> (short-covering squeeze — prone to fading).

`bt_oi_veto.py` mirrors `trend_follower.py`: shared book, MAX_POS=3, entry =
coin>SMA50 AND BTC>SMA200, ranked by 30d momentum, fee 0.1%/side, IS/OOS 60/40,
fold signs before totals, per-trade decomposition.

## Result — REJECT as a deployable gate (the veto is real but worthless to us)

Book-level (1000 aligned days):

```
baseline   ret +88.0%  n=171  OOS -9.8%   folds pos 3/6  best2share 176%
OI-VETO    ret +174%   n=121  OOS +13.8%  folds pos 4/6  best2share 170%
BTC buy&hold (same window): +31.5%
```

The book-level "improvement" is **not an edge**: best2share is ~170% in BOTH
variants — the same two trades (DOGE +229%, XRP +154%, the Dec-2024 alt season)
carry more than the entire result. Everything else combined is **−165% baseline
/ −158% OI-veto** — negative either way. A ZEC-shaped result, rejected on the
§14.2/Trap-3 decomposition rule.

**Trade-level (the honest, informative cut):** entries bucketed by whether a
squeeze was active at entry —

```
                n    WR     mean      median
squeeze        91   33.0%  -3.32%    -2.34%
clean          79   49.4%  +6.65%    -0.10%   <- winsorized ±20%: mean +0.85%
IS  squeeze    79   34.2%  -3.69%    -2.57%
OOS squeeze    12   25.0%  -0.87%    -2.15%
```

The squeeze flag **is** a robust loss-avoider at the entry level — 33% WR,
negative in BOTH folds, median −2.3% — exactly the "veto, not signal generator"
role §4 predicted. **But** the clean bucket's median is ≈ 0, i.e. the underlying
trend book has no positive edge to protect. Gating on the flag only makes us
**trade less**, not profit more. Unconditional BTC 5d event study confirms the
effect is NOT a market-timing signal (squeeze +0.41% vs clean +0.24%).

⇒ **`okx_oi_logger.py` is MONITOR-ONLY. Do NOT wire the veto into the live entry
path.** The reusable artifacts are the fetcher + the now-backtestable OI series
(any future OI idea can be validated rather than assumed), not the veto.

## Files

- `src/trading/signals/okx_oi.py` — public fetcher (no auth, no npm, VPS-safe)
- `tests/test_okx_oi.py` — 7 tests, no network
- `scripts/okx_oi_logger.py` — daily monitor-only snapshot → `data/okx-oi.jsonl`
- `research/oi/okx_oi_daily.json` — saved 1008-bar daily OI (BTC/ETH/SOL/DOGE)
- `research/oi/bt_oi_veto.py` — the backtest above (reproducible)
