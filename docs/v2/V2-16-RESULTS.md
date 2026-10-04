# V2-16 results: accuracy round 2 + production readiness

* Branch: `v2/production-readiness`. It is stacked on `v2/position-management`; `main` plus additive changes only.
* Release: branch `v2/release-rc1` (the session's git proxy refuses tag pushes; the commit SHA is the pin).
* Design and pre-registration: `docs/loop/designs/V2-16-accuracy-round-2-and-production.md`, committed before any
  result was seen.

## 1. Entry edge, round 2 (pre-registered): `docs/v2/evidence/entry_study_v2.json`

**Setup.**

* 15 coins, 1h bars, 2020-01..2026-08. Walk-forward out-of-sample years 2023 / 2024 / 2025 / 2026.
* Barrier: stop = 1.5 × daily ATR, target = 2 × stop, 7 days, managed with the shipped bracket + ratchet.
* Bonferroni over 6 candidates.

| candidate | trades | mean/trade | Bonferroni LB | per-year mean (23/24/25/26) | admitted |
|---|---|---|---|---|---|
| every decision point (benchmark) | 10,588 | -0.27% | -0.60% | +0.08 / -0.22 / -0.38 / -0.71 | – |
| F1 time-series momentum | 4,475 | -0.13% | -0.53% | +0.02 / +0.03 / -0.45 / -0.43 | no |
| F2 cross-sectional top third | 4,621 | -0.29% | -0.62% | +0.07 / -0.26 / -0.41 / -0.71 | no |
| F3 20-day breakout | 614 | +0.08% | -0.60% | +0.07 / -0.13 / +0.47 / +0.10 | no |
| F4 momentum + BTC regime | 4,030 | -0.05% | -0.49% | +0.03 / +0.05 / -0.22 / -0.41 | no |
| F5 RSI reversion | 741 | -0.12% | -1.10% | +0.83 / +0.36 / +1.13 / -1.68 | no |
| M walk-forward logistic | 257 | **+0.21%** | -0.83% | **+0.11 / +0.04 / +0.52 / +3.28** | no |

**What it means.**

* **No entry rule clears the bar.** Momentum, cross-sectional strength and breakouts all lose after costs on
  2023-26.
* **M is the only promising candidate.**
  * Its probabilities have real out-of-sample skill: AUC 0.60 / 0.55 / 0.54 / 0.53 and pooled Brier skill +0.017,
    against 0.49 for the minute features in V2-15.
  * Its trades were positive in all four years.
  * But with 257 trades, the interval is too wide to admit it.
* **Buy-and-hold over the same span** ranged from -84% (ATOM) to +934% (SOL). Survivorship flatters this universe,
  which biases toward FINDING an edge. None was found.

## 2. The same information on the label the live bracket trades: `docs/v2/evidence/context_fit.json`

* The runtime context features (`trading.v2.daily_context`, identical code in the fit and live) were refit
  walk-forward on the approved +7% / -3.5% / 14-day label.
  * AUC by year: 0.60 (2023), then 0.50 / 0.52 / 0.50.
  * Pooled: AUC 0.528, Brier skill -0.003.
  * Trading: 2,039 trades, +0.13% per trade, Bonferroni LB -0.30%. Not admitted.
* The skill belongs to the volatility-scaled 7-day barrier, not to the fixed one.
* The gateway's honesty rule now requires BOTH ranking skill (AUC) and calibration skill (Brier) before it may
  move off the base rate. This context model gets shrink 0 and is NOT wired into the gateway.
* **Owner question (E16):** volatility-scaled stops and targets showed more predictable outcomes. Changing the
  approved 3.5% / 7% is a risk-limit decision, so it was not made.

## 3. What did change for accuracy

* **Jev now sees multi-day context** (`features-v2` = features-v1 + `context-v1`):
  * returns over 1-30 days in volatility units;
  * distance from the 20- and 50-day averages;
  * the cross-sectional rank among 15 coins;
  * the BTC regime;
  * daily RSI and ATR.
* Workflow `crypto_trading_v3` explains the block. It is verified live: 3/3 jev-1.13.0 calls with the full
  15-coin context, at about 2.6k input tokens per call (+45% cost).
* **The scorecard** (`python -m trading.v2.scorecard --out <dir>`) grades every logged decision afterwards, per
  source. "Is Jev accurate? Does the gateway rival it?" now has a number that grows every day:
  * forward returns at 1h / 4h / 24h;
  * Brier, AUC and calibration of P(LONG) and expected_edge against the barrier;
  * the 24h directional hit rate;
  * the Jev × gateway agreement;
  * exit-versus-hold precision for position advice.

## 4. Production readiness

| piece | what |
|---|---|
| `trading.v2.preflight` | 12 checks (Python, dependencies, wire-ready workflows, gateway model, approved config, key present, writable, disk, chains verify, no order path anywhere in `trading.v2`, Binance reachability + clock skew, TypeSafe reachability); exit 1 blocks the start |
| `trading.v2.notify` | Telegram via the box's `hermes send`; never raises; redacts credentials; rate limits per key |
| `trading.v2.watchdog` | every 5 minutes: runner silent, kill engaged, Jev failing, feed failing, disk low, broken chain, cycle errors; alerts on each transition and on recovery; daily digest |
| `deploy/v2/` | `run-v2.sh` (preflight, then the runner), `ops-v2.sh`, a separate `ecosystem.v2.config.js` (v2-paper, v2-watchdog, v2-daily; memory cap, SIGINT grace), `v2.env.example` |
| isolation | an isolated git worktree `/opt/trading-agent-v2` at a release tag; the live checkout, its processes, `main`, `LIVE_ENABLED` and the old ecosystem file are untouched |
| docs | `RUNBOOK.md` (operate, upgrade, roll back, rotate the key, alerts, live-readiness gate) and `VPS-AGENT-PROMPT.md` (paste to the VPS agent) |

**End-to-end smoke test** through the real `run-v2.sh`, with a fake VPS root, a real Jev key and real Binance
data: see section 5.

## 5. Smoke test (through the real `deploy/v2/run-v2.sh`)

**Setup:** a fake VPS root, the real Jev key, live Binance data, 3 symbols, a 1-minute cadence, 4 minutes.

**Results:**

* Preflight 12/12 PASS. The runner started.
* 15 cycles: 12 real jev-1.13.0 decisions (12/12 OK, 0 fallbacks; NO_TRADE at 0.61-0.65, IGNORE tier).
* Multi-day context present on 15/15 snapshots. The gateway dual-run agreed 12/12.
* 0 feed failures. The kill switch was not engaged. All chains verify.
* Jev cost: about 31k input / 3.5k output tokens for 12 calls.

**Notable:**

* The 3 cycles at the first boundary after priming were `snapshot_unusable` (stale). Priming the 15-coin context
  took about 80 s on the sandbox network, so those bars were refused rather than used late. That is correct, and
  it costs only the first boundary after a (re)start.
* `ops-v2.sh daily` produced the scorecard (all 12 decisions pending: nothing is 14 days old yet) and the digest.

**Audit finding from this run:** the watchdog marked an alert as reported even when Telegram was unreachable,
so a real problem could go unreported forever. It now retries undelivered alerts every run (2 tests).

## 6. Mistakes in this round, and what was done about them

* A commit (5aa937d) went in with one failing test: `| tail` masked pytest's exit code. It was fixed in the next
  commit. Every later commit runs under `set -o pipefail`.
* The probe's first live attempt used a 60 s staleness ceiling, which the sandbox's slow fetches exceeded. That was
  a probe setting, not a product bug. The runner's own ceiling and the first-cycle staleness behaviour are reported
  in section 5.

## 7. Open (owner)

* **E13:** the position thresholds are unvalidated.
* **E14:** exits during a kill.
* **E15/E16:** the entry edge; volatility-scaled exits.
* VPS deployment: paste `docs/v2/VPS-AGENT-PROMPT.md`.
* Rotate the development key before deployment.
