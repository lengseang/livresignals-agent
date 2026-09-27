# ROADMAP v3 · Strategy factory: scavenge → validate → shadow-scan → display

Status: **plan, waiting on the owner.** Nothing here places an order. Promotion to real money
keeps the existing rules (design first, owner approves, one change per PR).

## Why

Our own strategies have not passed the gate (2026-09-27: 0 PASS). Building more from scratch
is slow and the intraday family is dead after the ~0.28% round-trip cost. Published strategies
are cheaper candidates, but they decay: returns are ~26% lower out-of-sample and ~58% lower
after publication (McLean & Pontiff, JF 2016). So a borrowed strategy gets no free pass: it
goes through the same gate, and **every candidate counts as a trial** in the Deflated Sharpe.

What we can own is not the signal, it is the **process**: pre-registered rules, a global trial
count, forward shadow records that are committed before outcomes are known, and a live display.

## The pipeline

```
 SCAVENGE          PRE-REGISTER        VALIDATE (history)        SHADOW (live market)       DISPLAY
 candidate card -> frozen spec    ->   lab runner: IS/OOS,   ->  scanner runs every     ->  feed -> terminal
 source, claim,    params from the     walk-forward, daily       non-rejected candidate     league panel,
 data, costs,      source, hashed,     DSR (global N), costs,    on recorder data, no       markers per
 license, prior    trial counted       vs BTC hold + EW basket   orders; every signal       strategy,
                                       -> REJECT / SHADOW        hash-chained BEFORE its    forward scorecards
                                                                 outcome; forward scorer
                                                                 -> PROMOTE? (owner)
```

Stages and who decides:

| Stage | Output | Gate to next stage | Automatic? |
|---|---|---|---|
| Scavenge | `research/candidates/<id>.json` card | card complete (source, rules, data, cost sensitivity, license) | research task |
| Pre-register | frozen lab spec, hash, trial row | params taken from the source, not tuned | yes |
| Validate | gate.json per run | beats **both** benchmarks after measured costs, daily DSR ≥ 0.95 with global N | yes (lab runner, overnight) |
| Shadow | append-only, hash-chained shadow signal log | ≥ 60 forward days **and** ≥ 30 signals, forward DSR and benchmark pass | yes (24/7 on the PC, later VPS) |
| Holdout | one use per candidate (2026-03 onward) | pass once, no retries | yes |
| Real | trading design | owner approval, small size | **no, owner only** |

Candidates that fail validation still shadow-scan in a separate "rejected" lane at zero cost:
their forward record tells us whether the rejection was right.

## Candidate list (pre-registration order)

Tier A needs only data we already record (spot klines, per-second ticks).

| id | Candidate | Source | Claim (as published) | Our use | Prior |
|---|---|---|---|---|---|
| A0 | **Benchmarks**: BTC buy-and-hold, equal-weight basket | — | — | the bar every candidate must clear | — |
| A1 | Donchian ensemble + volatility sizing, top-liquid rotation | Zarattini, Pagani, Barbon, SSRN 2025 | Sharpe > 1.5, alpha 10.8%/yr vs BTC | direct upgrade of trend_follower | medium-low (not peer-reviewed) |
| A2 | CTREND-lite: cross-sectional trend score, long top-k weekly | Fieberg et al., JFQA 2025 | survives costs in big liquid coins | long-only on 15→38 coins | medium (peer-reviewed) |
| A3 | Risk-managed time-series momentum, crash de-risking | Howden & Andreev, SSRN | walk-forward Sharpe 1.41 net 25 bp vs 0.70 hold, drawdown halved | risk overlay for A1/A2 | medium-low |
| A4 | Nearness to 52-week high | J. Banking & Finance 2025 (anchoring) | ~130 bp/week long-short spread | long-only top bucket | low-medium |
| A5 | MAX (lottery) effect | Financial Innovation 2021 | high-MAX coins underperform next month | **filter**: skip high-MAX coins on entry | medium |
| A6 | Cross-sectional momentum | Liu, Tsyvinski, Wu, JF 2022 | market/size/momentum factors | control for A2 | low (heavily published) |
| A7 | BTC → altcoin lead-lag | J. Econ. Dynamics & Control 2024; Asia-Pac. Fin. Markets 2026 | alts react late to BTC; net-of-cost OOS returns | uses our per-second recorder, our one data edge | low (cost-sensitive) |

Tier B needs new **read-only** data; license check before any use.

| id | Candidate | Source | Claim | Our use | Data |
|---|---|---|---|---|---|
| B1 | Token-unlock drift | SSRN (52 Binance unlocks); Keyrock 16k unlocks; unlocks.app 236 events | −14.7% one-month pre-event drift; 1% unlock ≈ −0.3% week before and after | **filter/exit**: don't hold into large unlocks | unlock schedules (license?) |
| B2 | Funding-rate extremes | practitioner evidence only | extremes mark crowding, precede reversals | regime filter | Binance futures market data (read-only) |
| B3 | Stablecoin exchange inflows | IMF WP 2026/056 | significant predictor of BTC returns | BTC regime input | CryptoQuant/Glassnode (paid) or on-chain |

Rejected on evidence: crypto carry (Sharpe turned negative in 2025, needs futures),
intraday seasonality (localized, killed by our costs), large freqtrade bots (hundreds of
tuned rules, no verified live record, GPL).

Filters (A5, B1, B2) are layered on a core strategy instead of standing alone: fewer trials,
and they fit a spot, long-only, small account.

## Phases

| Phase | Work | Runs where | Owner action |
|---|---|---|---|
| P0 | candidate cards, **global trial registry**, benchmark specs (A0) | PC, lab runner | none |
| P1 | plugins A1–A4, validator adds the benchmark rule; one overnight batch | PC, lab runner | review results |
| P2 | shadow scanner: plugin host, hash-chained shadow log, forward scorer | PC 24/7 (LIVR-Recorder host) | approve design (touches scanner) |
| P3 | feed events `candidate_status`, `shadow_signal`, `forward_scorecard`; terminal league panel | PC; terminal branch | none |
| P4 | Tier B read-only data connectors (license first), filters A5/B1/B2 | PC | approve data sources and cost |
| P5 | promotion design for the first candidate that clears shadow + holdout | — | **owner decides** |
| later | move feed + shadow scanner to the VPS (deploy plan B) | VPS | owner deploys |

## Business angle

- **The shadow league is the product wedge.** "We ran famous crypto strategies live, committed
  every signal before the outcome; here is what actually happened" is shareable content that no
  signal seller can fake. It answers *how they find it* even if no strategy passes.
- Paid signals only from strategies that clear shadow + holdout.
- **Must check before selling signals:** in several jurisdictions this can be regulated
  investment advice. Get that answered before any paid launch.

## Pre-mortem: it's 3 months later and this failed. Why?

- **Something passed by luck.** Many candidates means some look good by chance. Mitigation: global
  N in the DSR, one-shot holdout, 60 forward days.
- **Shadow looked good, real fills didn't.** Mitigation: measured spreads per coin; the real
  record (`real.closed`, F3) is compared against shadow for the same signals.
- **Data gaps** when the PC sleeps. Gaps are recorded, never filled; affected days are excluded
  and disclosed.
- **Weeks of infrastructure, no edge.** The league content and the verified record still have
  value; stop scavenging if the reverse-if below triggers.

**Reverse if:** after 90 forward days no candidate beats BTC buy-and-hold net of costs →
stop scavenging and pitch the trend family as risk reduction ("similar exposure, a fraction of
the drawdown"), which is what the independent evidence supports.

## Sources

- McLean & Pontiff, JF 2016: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2156623
- Zarattini, Pagani, Barbon, SSRN 2025: https://papers.ssrn.com/sol3/Delivery.cfm/5209907.pdf?abstractid=5209907&mirid=1
- Fieberg et al., JFQA 2025: https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/trend-factor-for-the-cross-section-of-cryptocurrency-returns/4C1509ACBA33D5DCAF0AC24379148178
- Howden & Andreev, SSRN: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7115459
- Anchoring / 52-week high, JBF 2025: https://www.sciencedirect.com/science/article/abs/pii/S0378426625002122
- MAX effect, Financial Innovation 2021: https://jfin-swufe.springeropen.com/articles/10.1186/s40854-021-00291-9
- Liu, Tsyvinski, Wu, JF 2022: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3379131
- Cross-crypto predictability, JEDC 2024: https://www.sciencedirect.com/science/article/abs/pii/S0165188924000551
- BTC → altcoin transmission, APFM 2026: https://link.springer.com/article/10.1007/s10690-026-09589-z
- Token unlocks: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6632838 · https://keyrock.com/from-locked-to-liquidity-what-16000-token-unlocks-teach-us/ · https://insights.unlocks.app/do-token-unlocks-crash-prices/
- Stablecoin inflows, IMF WP 2026/056: https://www.elibrary.imf.org/view/journals/001/2026/056/article-A001-en.xml
- Crypto carry, BIS WP 1087: https://www.bis.org/publications/working-paper-1087-crypto-carry · 2025 performance: https://arxiv.org/pdf/2510.14435
- Independent trend test: https://summitward.com/learn/crypto-trend-following
