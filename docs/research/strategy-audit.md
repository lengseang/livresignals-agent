# Strategy audit: published ideas against our five rules

Task T1.3 · 2026-09-25 · Inputs: [fees-and-breakeven.md](fees-and-breakeven.md) (cost floor),
[sources.md](sources.md) (what data we can actually get), `SYSTEM_MAP.md` §4–5 (our own evidence).

## How to read this

Our five rules (SYSTEM_MAP §4): **(1)** shared-account simulation, **(2)** IS/OOS split, **(3)** fees
and slippage included, **(4)** beat BTC buy-and-hold, **(5)** name the regime of the test period.
Academic papers almost never satisfy (1) or (3) at *our* cost level (≈0.14–0.20% round trip, VIP 0).
So a paper's result is a **hypothesis to test**, never evidence of edge. The verdict column
says what we do with it:

- **TEST**: worth building and gating in Phase 3/4.
- **FILTER**: not a standalone strategy at our costs, but useful as an entry-timing or quality input.
- **BASELINE**: already running; register it so new ideas are compared against it.
- **DEFER**: needs data we don't have yet (see sources.md).
- **SKIP**: fails on cost or on our own prior evidence.

## Verdicts

| # | Idea | Source | What it claims | Our constraint | Verdict |
|---|---|---|---|---|---|
| 1 | Order-flow imbalance (OFI) at the best bid/ask | Cont, Kukanov, Stoikov, *The Price Impact of Order Book Events*, J. Financial Econometrics 2014 ([arXiv](https://arxiv.org/abs/1011.6402)) | Short-horizon price changes are ~linear in OFI, slope inversely proportional to depth; strongest over tens of seconds | Predicted moves are on the order of ticks, far below our ~0.2% round-trip cost; as a taker strategy it pays the spread it predicts | **FILTER**: time entries of longer-horizon signals, and place maker entries on the favourable side. Needs `@bookTicker` + `@aggTrade` (available) |
| 2 | Intraday momentum and reversal | Wen, Bouri, Xu, Zhao, *Intraday return predictability in the cryptocurrency markets: momentum, reversal, or both*, N. Am. J. Econ. & Finance 2022 ([SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4080253)) | Both effects exist in BTC (2013–2020) and in ETH/LTC/XRP; timing strategies beat buy-and-hold | The sample ends in 2020 (a different regime); the value is before our fees | **TEST** at hourly horizons with TP ≥ 1% (the viable zone from T1.1) |
| 3 | Time-series momentum, 1–4 weeks | Liu & Tsyvinski, *Risks and Returns of Cryptocurrency*, RFS 34(6) 2021 ([OUP](https://academic.oup.com/rfs/article-abstract/34/6/2689/5912024)) | Strong time-series momentum over 1–4 week horizons | Low turnover, so fees matter little. Our own trend follower (SMA50 + BTC filter) is this family | **BASELINE**: port `trend_follower`'s signal as the benchmark strategy (T3.4). Note: our *cross-sectional* momentum failed (+116% IS → −80.6% OOS, SYSTEM_MAP §5); that's a different signal |
| 4 | Investor attention (search volume) | same paper | High attention predicts higher 1–2 week BTC returns | Google Trends isn't in our tested sources | **DEFER** |
| 5 | Funding-rate carry (long spot / short perp) | e.g. [Predictability of Funding Rates (SSRN 5576424)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5576424); a search-result summary (primary source **not verified**) reports carry Sharpe falling sharply in 2024 and turning negative in 2025 | Funding is partly predictable; carry profits come from funding | Needs a futures account, both legs, and capital; live funding isn't reachable from dev; the reported edge decayed | **SKIP** for now; revisit only if E6(d) gives VPS access and the account grows |
| 6 | Option-expiry reversal (BTC) | Finance Research Letters 2026 (ScienceDirect pii S1544612326008688; abstract-level only) | Reversal around expiry when ATM open interest is high | Needs options OI/gamma (Deribit); not in our sources | **DEFER** |
| 7 | Short-horizon mean reversion (VWAP/Bollinger, 1–15m) as a taker | common practice; our own evidence | — | Our HF/scalping tests lost 30–57% to fees (SYSTEM_MAP §5); T1.1 shows TP ≤ 0.3% needs 74–90%+ win rate | **SKIP as taker.** TEST only as a **maker-entry** variant, with the fill model's non-fill risk (T4.1) |
| 8 | Range breakout with volume confirmation, 1h–4h | common practice | — | Natural TP ≥ 1%; data available (klines, aggTrade volume) | **TEST** |
| 9 | Regime-adaptive long/short | our own research (SYSTEM_MAP §5: OOS +40.8%, DD −21.9%, in a bear market) | — | Shorts need futures/margin; the paper engine can simulate it; "not yet deployed anywhere" | **TEST** on paper (long side on spot, short side paper-only) |
| 10 | Capitulation dip-buy (`adaptive_engine`) | our own repo | Runbook says ~70% win rate, +0.44% EV, PF 2.92 (`docs/ops-runbook.md` §3) | SYSTEM_MAP §5 says "dip-buying: 66% win rate still LOSES". **The repo contradicts itself** | **BASELINE, pending T1.4**: reconcile both claims against their evidence before trusting either |

## The gate every idea must pass (for T3.3)

Testing many variants makes some look good by chance. The gate uses the **Deflated Sharpe Ratio**
(Bailey & López de Prado, 2014, [SSRN 2460551](https://ssrn.com/abstract=2460551)), which adjusts the
significance of a Sharpe ratio for the number of trials, skewness and kurtosis. Every variant
the generator (T3.5) produces counts as a trial. In addition to DSR, a strategy must:

1. run in the shared-account simulator with fees from `fee-audit.json` (or the VIP 0 scenario until known) plus measured spread;
2. be positive **out of sample** on a chronological split;
3. beat BTC buy-and-hold over the same window, or state clearly that it doesn't;
4. report the test window's regime (trend/range, high/low vol);
5. report its win rate **next to its own break-even win rate** p* (T1.1).

## What gets built first (feeds T3.4 / T4.3)

1. Baselines: trend_follower signal (#3), the adaptive capitulation signal (#10, after T1.4).
2. New tests: intraday momentum/reversal at hourly horizons (#2), range breakout (#8), regime-adaptive L/S on paper (#9).
3. Filters: OFI entry timing (#1), available to any strategy once the tick stream (T2.2) exists.
4. Maker-entry mean reversion (#7): only after the fill model models non-fills.

## Limits of this audit

- Summaries of papers came from abstracts and search snippets, not full-text reading. Claims are
  stated at the level the abstracts support. Before a strategy is built, its paper gets a full read.
- Nothing here is evidence that any strategy is profitable for us. That's what the gate is for.
