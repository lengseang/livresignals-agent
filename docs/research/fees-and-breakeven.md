# Fees and break-even: what a trade must win just to pay costs

Task T1.1 · 2026-09-25 · Source script: `research/07-breakeven.py` (tested in `tests/loop/test_breakeven.py`)

## Bottom line

At this account's tier, **a symmetric scalp with a take-profit of 0.3% or less needs a win
rate of roughly 74–90%+ just to break even**, and at 0.2% it's close to impossible. Even the
repo's most optimistic fee assumption doesn't change that. Scalping that can survive fees here
needs either **targets of about 1% or more**, or an **asymmetric exit** (win bigger than lose),
or **entries that don't pay the spread** (limit orders). "Hundreds of trades an hour" at small
targets is ruled out by arithmetic before any strategy is tested.

## What is known, and what isn't

| Fact | Value | Status |
|---|---|---|
| Regular (VIP 0) spot fee | 0.100% maker and taker | secondary sources agree ([bitdegree](https://www.bitdegree.org/crypto/tutorials/binance-fees), [tradersunion](https://tradersunion.com/brokers/crypto/view/binance/fees/)); binance.com itself refuses connections from this machine |
| BNB fee discount | exists, applied as a rate on standard commission | primary: [Binance spot API docs](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md) (`discount` block). The docs' own examples disagree (`0.25` on order responses, `0.75` on the commission endpoint), so **the actual rate for this account is UNKNOWN** |
| This account's real rates | from `GET /api/v3/account/commission` per symbol | **UNKNOWN**: needs the account (E1) or `data/fee-audit.json`, which measures fees from real fills |
| Repo assumption | 0.144% round trip (0.072%/side) | used in `research/01-03, 06` and `SYSTEM_MAP.md` §4. Its comment says "BNB-discounted spot" (`research/01-families.py:19`), with no measurement cited; `fee_audit.py` exists to replace it with a measured value |
| Spread and slippage per pair | not measured | **UNKNOWN** until T2.3 records real book data |

## The formula

For take-profit `tp`, stop-loss `sl`, fee `f` and slippage `s` per side (all fractions of price):

```
round-trip cost      c  = 2(f + s)
break-even win rate  p* = (sl + c) / (tp + sl)
```

At `p*`, expectancy is exactly zero after costs (proved by `test_expectancy_is_zero_at_the_breakeven_rate`).

## Break-even win rates

Output of `python research/07-breakeven.py`. "IMPOSS." means p* ≥ 100%.

**VIP 0, no BNB (0.100%/side)**

| TP / SL | slip 0 | slip 2bp | slip 5bp |
|---|---|---|---|
| 0.20 / 0.20% | IMPOSS. | IMPOSS. | IMPOSS. |
| 0.30 / 0.30% | 83.3% | 90.0% | IMPOSS. |
| 0.50 / 0.50% | 70.0% | 74.0% | 80.0% |
| 0.50 / 0.25% | 60.0% | 65.3% | 73.3% |
| 1.00 / 1.00% | 60.0% | 62.0% | 65.0% |
| 2.00 / 1.00% | 40.0% | 41.3% | 43.3% |

**Repo assumption (0.072%/side = 0.144% round trip)**

| TP / SL | slip 0 | slip 2bp | slip 5bp |
|---|---|---|---|
| 0.20 / 0.20% | 86.0% | 96.0% | IMPOSS. |
| 0.30 / 0.30% | 74.0% | 80.7% | 90.7% |
| 0.50 / 0.50% | 64.4% | 68.4% | 74.4% |
| 0.50 / 0.25% | 52.5% | 57.9% | 65.9% |
| 1.00 / 1.00% | 57.2% | 59.2% | 62.2% |
| 2.00 / 1.00% | 38.1% | 39.5% | 41.5% |

## What this means for the roadmap

1. **The scanner (Phase 3) must score signals net of cost.** A signal's expected move is compared with
   `c` for its pair *before* it's ranked. A signal whose expected move is below the pair's
   round-trip cost is never emitted as tradeable, only as "observed".
2. **Paper scalping (Phase 4) targets ≥ 1% or asymmetric R.** Strategies with TP ≤ 0.3% are only
   worth testing with maker (limit) entries, which avoid crossing the spread (`s → 0`) but
   risk not filling. The fill model (T4.1) must include that non-fill risk, or the paper results will
   flatter the strategy.
3. **"Win rate" is never reported alone.** Every strategy report prints its win rate next to its own
   `p*`. A 69% win rate is a loss at 0.5/0.5% with 5bp slip on VIP 0 (p* = 80%).
   That's why T1.5 re-checks the `unified_trader.py:68` claim (5m, 69% WR) against its actual TP/SL.
4. **Volume is not a lever.** At a fixed edge, more trades multiply the cost linearly. This matches
   `SYSTEM_MAP.md` §5 ("more trades = more loss, linearly").

## Open items

- Real fee rate: read `data/fee-audit.json` (already in the E1 request).
- Spread and slippage per pair: T2.3 (24h tick recording) measures them; this table is then re-run with real `s`.
