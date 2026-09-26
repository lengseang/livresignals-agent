# Measured spreads and top-of-book depth (a 7.6-hour sample)

Task T4.1 · 2026-09-26 · Source script: `research/08-spread-stats.py` (tested in `tests/loop/test_spread_stats.py`)

> **This is a sample of 7.56 hours on one evening (UTC), not a long-run estimate.** Spreads widen in
> fast markets, at the weekly open and around news, and nothing guarantees any of those fell in this window.
> Re-run the script as more closed hours accumulate and replace the tables.

## Bottom line

- **On all 15 paper symbols the spread sits at its minimum (about one price tick) 89–100% of the time.**
  Tick size ÷ price sets the relative spread, not market stress: BTC 0.0012 bp and ETH 0.037 bp, but
  DOT 8.4 bp, ATOM 5.6 bp, ARB 4.5 bp and ADA 3.9 bp. The expensive ones are the low-priced coins with coarse ticks.
- **For the majors the fee is the whole cost.** Crossing half the spread costs 0.0006 bp per side on BTC and
  0.48 bp per side on the median symbol, against a 10 bp/side fee at VIP 0. The 2 bp and 5 bp "slip" columns
  in `fees-and-breakeven.md` were **too pessimistic for the majors and about right for DOT, ARB and ATOM**.
- **A $5 order fits in L1 essentially always** (larger than L1 at ≤ 0.6% of real trades). **A $50 order doesn't always fit**: it's
  larger than the level it would take at 13–25% of real trades on BTC, ETH, BNB, AVAX, LINK and SUI.
  Beyond L1 the price is **UNKNOWN**, because the recording has no depth past the best level.

## How it was measured

```
PYTHONPATH=src .venv/Scripts/python.exe research/08-spread-stats.py \
    --root D:/1111-Works/LIVRsignals/livresignals-agent/data/local/ticks --breakeven
```

- Input: the **closed** hourly files only, `ticks/2026-09-25/{book,trade}/17..23.jsonl.gz` and
  `ticks/2026-09-26/{book,trade}/00.jsonl.gz` (8 files each). The script skips the hour still being recorded.
- Window (receive clock, UTC): **2026-09-25 17:26:13 → 2026-09-26 00:59:59 = 7.56 h**. The recorder has
  been running longer, but only these hours were closed when the script ran (2026-09-26 01:26 UTC).
- Source: Binance spot `bookTicker` + `aggTrade` from `data-stream.binance.vision` (the T2.1/T2.2 recorder).
- **Time-weighted.** Each kept book record counts for as long as it stood, capped at 5 s so a gap or a
  reconnect can't stretch one quote over minutes. 2,994 intervals were capped, mostly on quiet symbols,
  which is why ATOM shows only 6.13 observed hours. The event-weighted median sits next to it: the two agree
  within 0.02 bp, so the weighting choice doesn't matter here.
- **Recorder filter.** The recorder keeps only updates that move a best *price*, plus about one snapshot per second
  (T2.1). Quantity-only changes in between are lost, so **L1 depth is a sampled approximation**. Spreads are
  exact at every kept record, because every price change is kept.
- Locked or crossed books (bid ≥ ask): 0 records.

## Spreads and L1 depth

| Symbol | book records | hours observed | median bp | p90 bp | p99 bp | median bp (event-weighted) | time at min observed spread (~1 tick) | median bid depth $ | median ask depth $ |
|---|---|---|---|---|---|---|---|---|---|
| ADAUSDT | 21,921 | 7.38 | 3.9162 | 3.9424 | 3.9549 | 3.9193 | 100.0% | 11,359 | 10,995 |
| ARBUSDT | 40,212 | 7.45 | 4.4573 | 8.8261 | 9.0457 | 4.4753 | 88.8% | 2,082 | 1,983 |
| ATOMUSDT | 14,632 | 6.13 | 5.6386 | 5.7029 | 11.3379 | 5.6609 | 90.4% | 930 | 479 |
| AVAXUSDT | 54,524 | 7.39 | 0.9512 | 0.9605 | 1.9185 | 0.9572 | 94.9% | 465 | 550 |
| BNBUSDT | 29,442 | 7.45 | 0.1290 | 0.1294 | 0.1296 | 0.1291 | 100.0% | 7,670 | 7,576 |
| BTCUSDT | 45,271 | 7.50 | 0.0012 | 0.0012 | 0.0012 | 0.0012 | 99.9% | 271,637 | 175,324 |
| DOGEUSDT | 40,447 | 7.49 | 1.0187 | 1.0271 | 1.0317 | 1.0220 | 100.0% | 5,719 | 5,215 |
| DOTUSDT | 17,995 | 7.26 | 8.3717 | 8.4782 | 8.5288 | 8.3857 | 100.0% | 4,138 | 3,929 |
| ETHUSDT | 50,350 | 7.49 | 0.0372 | 0.0373 | 0.0373 | 0.0372 | 99.9% | 48,704 | 38,932 |
| LINKUSDT | 47,003 | 7.39 | 0.7211 | 0.7262 | 0.7283 | 0.7243 | 99.9% | 695 | 759 |
| LTCUSDT | 39,189 | 7.43 | 1.4054 | 1.4264 | 1.4392 | 1.4091 | 100.0% | 1,884 | 1,886 |
| NEARUSDT | 60,805 | 7.48 | 2.0094 | 2.0315 | 2.0477 | 2.0167 | 100.0% | 2,130 | 2,001 |
| SOLUSDT | 35,321 | 7.48 | 0.8203 | 0.8263 | 0.8291 | 0.8212 | 100.0% | 17,199 | 18,713 |
| SUIUSDT | 137,532 | 7.49 | 0.8611 | 0.8863 | 0.8951 | 0.8806 | 99.9% | 691 | 780 |
| XRPUSDT | 46,412 | 7.49 | 0.6380 | 0.6428 | 0.6441 | 0.6390 | 100.0% | 12,441 | 12,623 |

"Time at min observed spread" is the share of time the spread equals the smallest spread seen for that
symbol. The tick size isn't in the recording, so read the column as "≈ 1 tick", not as a checked exchange fact.

## Taker fit: does the order fit in the top level?

Rule: a taker order fills at the opposite best price, with **0 extra slippage when its notional is at most
the L1 quantity** on that side. When the order is larger, the script only raises the flag **"exceeds top level"**. The
price of the remainder is UNKNOWN, because nothing beyond L1 is recorded.

There are two views. "% time" samples the book at every instant (time-weighted). "At real trades" looks up the latest
book record received before each real trade, on the side that trade's taker hit.

| Symbol | trades matched | $5 buy, % time | $5 sell, % time | $50 buy, % time | $50 sell, % time | $5 at real trades | $50 at real trades |
|---|---|---|---|---|---|---|---|
| ADAUSDT | 14,518 | 0.0% | 0.0% | 0.4% | 0.3% | 0.1% | 1.7% |
| ARBUSDT | 17,551 | 0.1% | 0.0% | 3.4% | 2.5% | 0.0% | 2.2% |
| ATOMUSDT | 6,356 | 0.0% | 0.0% | 2.2% | 1.0% | 0.1% | 1.8% |
| AVAXUSDT | 42,793 | 0.1% | 0.2% | 12.7% | 21.2% | 0.6% | 23.2% |
| BNBUSDT | 39,343 | 0.1% | 0.0% | 3.2% | 2.8% | 0.4% | 17.5% |
| BTCUSDT | 142,699 | 0.0% | 0.0% | 0.7% | 0.4% | 0.2% | 13.3% |
| DOGEUSDT | 59,328 | 0.3% | 0.3% | 1.2% | 1.3% | 0.6% | 3.4% |
| DOTUSDT | 9,932 | 0.1% | 0.0% | 2.1% | 1.6% | 0.1% | 7.7% |
| ETHUSDT | 95,418 | 0.0% | 0.0% | 1.3% | 1.2% | 0.3% | 19.6% |
| LINKUSDT | 38,192 | 0.1% | 0.1% | 10.9% | 13.8% | 0.3% | 25.3% |
| LTCUSDT | 54,083 | 0.0% | 0.0% | 2.1% | 2.6% | 0.0% | 5.3% |
| NEARUSDT | 61,073 | 0.1% | 0.1% | 2.0% | 2.1% | 0.1% | 3.8% |
| SOLUSDT | 58,304 | 0.0% | 0.1% | 0.2% | 0.2% | 0.0% | 0.7% |
| SUIUSDT | 138,244 | 0.1% | 0.1% | 11.2% | 9.9% | 0.2% | 20.1% |
| XRPUSDT | 193,009 | 0.0% | 0.0% | 1.6% | 1.1% | 0.0% | 5.7% |

**Real trades see thinner books than a random instant.** On BTC, a $50 order is larger than L1 0.4–0.7% of the time, but at 13.3% of real trades.
Two causes are mixed together here and not separated. First, trades happen while the level is being consumed, which is a real effect a fill
model must respect. Second, the bookTicker update caused by a trade can arrive *before* the trade message, so some matches
see the level already depleted, a measurement bias that overstates the flag. Treat the "at real trades" column as an upper bound.

## Latency

```
latency: median raw lag (recv_local - trade_ms) 5708 ms; median clock offset (server - local) -5332 ms over 49 samples (median RTT 669 ms, so offset uncertainty ~ +/-334 ms); implied one-way latency 376 ms
```

The local clock runs about **5.3 s ahead** of Binance, per `data/local/clock-offset.jsonl`, which is sampled about every 10 min
against Binance server time (`server_ms`). The offset cancels out of every spread and duration above, because both ends use the
local clock. It does **not** cancel out of latency. The implied one-way feed latency is **~376 ms ± ~334 ms**,
where the error is half the median REST round trip of the offset probe. So all that's known is that latency is
"hundreds of ms, under about 0.7 s". T4.1 treats it as a parameter, not a fact.

## Break-even with measured spreads

This re-runs the formula from `research/07-breakeven.py` with **slip per side = half the measured median spread**. A
taker round trip crosses half the spread on entry and half on exit, one full spread in total. The result is a
**lower bound** on cost: it leaves out slippage beyond L1, price drift during latency, and partial fills.

**VIP0 no BNB (0.100%/side)** (slip per side = half the measured spread)

| TP / SL | slip 0 | tightest median (BTCUSDT 0.0012bp) | median of symbols (0.95bp) | widest median (DOTUSDT 8.37bp) | widest p90 (ARBUSDT 8.83bp) |
|---|---|---|---|---|---|
| 0.20 / 0.20% | IMPOSS. | IMPOSS. | IMPOSS. | IMPOSS. | IMPOSS. |
| 0.30 / 0.30% | 83.3% | 83.3% | 84.9% | 97.3% | 98.0% |
| 0.50 / 0.50% | 70.0% | 70.0% | 71.0% | 78.4% | 78.8% |
| 0.50 / 0.25% | 60.0% | 60.0% | 61.3% | 71.2% | 71.8% |
| 1.00 / 1.00% | 60.0% | 60.0% | 60.5% | 64.2% | 64.4% |
| 2.00 / 1.00% | 40.0% | 40.0% | 40.3% | 42.8% | 42.9% |

**repo assumption (0.072%/side = 0.144% RT)** (slip per side = half the measured spread)

| TP / SL | slip 0 | tightest median (BTCUSDT 0.0012bp) | median of symbols (0.95bp) | widest median (DOTUSDT 8.37bp) | widest p90 (ARBUSDT 8.83bp) |
|---|---|---|---|---|---|
| 0.20 / 0.20% | 86.0% | 86.0% | 88.4% | IMPOSS. | IMPOSS. |
| 0.30 / 0.30% | 74.0% | 74.0% | 75.6% | 88.0% | 88.7% |
| 0.50 / 0.50% | 64.4% | 64.4% | 65.4% | 72.8% | 73.2% |
| 0.50 / 0.25% | 52.5% | 52.5% | 53.8% | 63.7% | 64.3% |
| 1.00 / 1.00% | 57.2% | 57.2% | 57.7% | 61.4% | 61.6% |
| 2.00 / 1.00% | 38.1% | 38.1% | 38.5% | 40.9% | 41.1% |

What changes compared with `fees-and-breakeven.md`: for BTC, ETH, BNB, SOL and XRP the measured spread cost is at most
0.41 bp per side, so the **"slip 0" column is the realistic one** and its break-even bars stand (0.3/0.3%
needs 83.3% at VIP 0). On DOT the spread alone costs 4.2 bp per side, and a 0.3/0.3% scalp needs 97.3% at VIP 0.
**On the majors it's the fees, not the spreads, that rule out small-target taker scalping. On the coarse-tick coins both do.**

## Open items

- Real fee rate: still UNKNOWN (E1 / `data/fee-audit.json`).
- Depth beyond L1: not recorded. It's needed before any order above about $50 on the thin symbols can be modelled.
  An `@depth5` / `@depth10` recording would provide it, as a separate task.
- A longer sample (≥ 7 days, including a weekend open and a high-volatility day) is needed before these numbers are used
  as defaults rather than as a first measurement.
