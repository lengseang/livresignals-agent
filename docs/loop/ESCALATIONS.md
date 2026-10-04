# Escalations: waiting on the owner

Newest first. The owner answers inline under **Answer:**; the next iteration picks it up.

## E7 · Pause `futures_executor` until its signal is re-tested net of fees? (T1.5, PR #13)
It trades **3x real futures** (every 10 min in `crontab.backup`, no enable flag) on the 15m SHARP + EMA200 signal. The
only evidence is a gross study (its scripts have no fee handling): "SOL WR 51% → 61%, OOS +13.5%". At 3x, fees are
charged on 3x the margin. Recommendation: pause its cron line (your action) until the T3.3 gate re-tests it with fees.
Also: which crontab is live? (`crontab -l`, E1)
**Answer:**

## E6 · Source decisions (T1.2, PR #9)
(a) CryptoPanic is now paid-only: subscribe, or skip it (the RSS feeds are used instead)? (b) Reading public Telegram channels
via `t.me/s/<channel>` is reachable but a terms gray area; allow it, use the official Telegram API with your account, or skip?
(c) Reddit needs an OAuth app: create one or skip? (d) Can the VPS reach `fapi.binance.com` (live funding/OI)?
The dev machine can't. Default until answered: skip a, b and c; defer funding/OI.
**Answer:**

## E5 · Retire `live_executor manage` from cron? (T0.6, PR #6)
It runs every 10 min (`config/crontab.trade`). If `config/LIVE_ENABLED` ever comes back, its exit path sells
the **entire free balance** of coins the live trend book also holds (same wallet). Recommendation: remove that
cron line (crontab = your action) and keep the code marked retired. Yes/no?
**Answer:**

## E4 · Jev API: VERIFIED live (V2-8)  (partly answered, remaining items below)
Ran against the live API on 2026-10-04 with the owner's test key: the client, the reply parsing and the status handling all
work (see `docs/loop/designs/V2-8-jev-client.md`, "VERIFIED"). Still open:
1. Real price per million input tokens and your cadence (real cost is ~1,800 tokens/call, so $0.11/day to $1.09/day at 15-min x 15
   symbols depending on the price). Also: balance 402 / rate-limit 429 / overload 529 behaviour not yet observed.
2. Review the question text in `src/trading/v2/workflows/crypto_trading_v2.json` (it is the strategy), then flip `active.json` to v2.
3. Before production: rotate the key (you said you will); keep it in an environment secret, never in the repo.
4. After testing: set the environment's network access back from Full to Custom.
**Answer:**

## E5 · Retire `live_executor manage` from cron? (T0.6, PR #6)
It runs every 10 min (`config/crontab.trade`). If `config/LIVE_ENABLED` ever comes back, its exit path sells
the **entire free balance** of coins the live trend book also holds (same wallet). Recommendation: remove that
cron line (crontab = your action) and keep the code marked retired. Yes/no?
**Answer:**

## E4 · Jev API: what I found, and what I still need (V2-8)
**Found** (web search only; the vendor docs are blocked from the sandbox; see `docs/loop/designs/V2-8-jev-client.md`):
`POST https://api.typesafe.ai/v1/systemone`, `Authorization: Bearer <key>`, body `{state, model:"jev-latest", questions:{...}}`,
questions are `choice` (instructions + criteria) or `noul` (yes/no), replies carry `probabilities` and `confidence`, errors
401/402/422/429/529. I built the client against that. It is UNVERIFIED against the live API.
**Still needed from you, once, on a machine that can reach the API:**
1. `TYPESAFE_API_KEY=<your key> python -m trading.v2.jev_probe jev_probe_response.json` (one call, a fraction of a cent, synthetic
   state; never prints or saves the key). Paste me the printed report and the saved JSON.
2. If I am ever to run it from the cloud sandbox: the egress policy must allow `api.typesafe.ai`, and the key must be set as an
   environment secret. Do not paste it in chat.
3. Your per-million-token price (sources say $0.042 to $0.42) and your cadence, so cost is a number, not a range.
4. Review the question text in `src/trading/v2/workflows/crypto_trading_v2.json` (it is the strategy), then flip `active.json`.
**Answer:**

## E5 · Retire `live_executor manage` from cron? (T0.6, PR #6)
It runs every 10 min (`config/crontab.trade`). If `config/LIVE_ENABLED` ever comes back, its exit path sells
the **entire free balance** of coins the live trend book also holds (same wallet). Recommendation: remove that
cron line (crontab = your action) and keep the code marked retired. Yes/no?
**Answer:**

## E4 · Review the F1–F4 fix designs (PRs #3, #4, #5, #6)
`docs/loop/designs/T0.3.md` and `T0.4.md`. F2 is High: a failed sell currently leaves a real position with **no hard stop**. Both need your approval before T8.1 can implement it, plus two facts:
(a) the dust threshold for "this coin is held" (proposed $1); (b) the output of
`df -T /opt/trading-agent` (whether atomic rename + fsync behaves as the design assumes).
**Answer:**

## E3 · CI fix touches a guard script (T0.8)
`check-books.py` hardcodes `/opt/trading-agent/data/book-counts-snapshot.json`, so CI
fails before pytest runs. Proposal: read the base path from an env var, defaulting to the
current path so prod behaviour is unchanged. It is on the no-touch list, so it needs a yes.
**Answer:**

## E2 · `src/trading/data/` exists only on the VPS (T0.7)
Please supply read-only copies of `/opt/trading-agent/src/trading/data/*.py` (code, no
credentials). They get committed with a `!src/trading/data/` gitignore exception.
**Answer:**

## E1 · Work item 1 (live-record discrepancy): snapshot request
**Also needed (T1.5):** `data/unified-paper.json`, `data/futures-trades.json`, `data/exit-params.json`, and the source of `/tmp/live_fine.json` (3.6y study data, not in git).
**Lead (from T0.5):** the real trend book writes no closed record, and `fee_audit.py` /
`expectancy_vs_fee.py` report the 53-symbol *paper* log as "REAL $". First check with the
snapshot: does the reported real PnL match `trend-state.json` `log` × $5 rather than the wallet's trade history?
Read-only: `data/trend-state.json`, `data/live-trades.json`, `data/fee-audit.json`,
`reports/signal-ledger.json`, the last ~500 lines of `logs/trend.log` plus every `ERR`/`record dropped`
line, `crontab -l`, wallet quantities for the 3 open coins, and whether `config/LIVE_ENABLED` exists.
**Answer:**

## E4 · V2-8: the real TypeSafe (Jev) client needs facts only the owner can supply
The sandbox cannot reach `api.typesafe.ai` (egress proxy blocks it), so the adapter (V2-3) speaks an INTERNAL raw
reply shape and the wire format is not guessed. To build V2-8 I need: (a) the request/response schema of
`POST /v1/systemone` (question types, how per-option probabilities come back, error and "balance exhausted"
responses, auth header), (b) the host allowed in the egress policy, (c) an API key supplied by you in the
environment, never pasted in chat or committed. Until then V2 runs fallback-only or against a recorded fixture.
**Answer:**

## E5 · A fallback-only outage cannot close positions (V2-2/V2-3, by spec)
Contract E caps a fallback at STRATEGY_CHECK, so during a Jev outage a fallback EXIT/REDUCE is escalated, not executed.
Positions then rely on the protective stop placed with the entry. Options: keep as is (spec), or allow
risk-REDUCING fallback exits through policy. Your call; I changed nothing.
**Answer:**

## E6 · Latent bug in the guard layer: `src/trading/risk/breakers.py::_utc_week_start`
`day.replace(day=day.day - day.weekday())` raises `ValueError` whenever the weekday offset exceeds the day of the
month (verified: 2026-10-02 Fri). Nothing calls it today; V2 deliberately does not. The file is the immutable
guard layer ("do not edit without a human risk-approval"), so I did not touch it. Fix would be
`day - timedelta(days=day.weekday())`.
**Answer:**

## E7 · Risk v2 policy choices that are yours to confirm (V2-4)
(a) `daily_loss_cap_usd`: the live cap is -$1.00 today; what value should the V2 shadow/paper books use?
(b) Kill switch blocks reduce-only exits too (matches the note's "block new orders"). Keep?
(c) `reduce_fraction` = 0.5 for "reduce" intents (no spec value).
(d) Existing sizing (2% risk / stop distance) yields ~100% of equity notional at a 2% stop; confirm the exit
    params you intend, since stop_pct drives size.
**Answer:**

## E8 · The legacy "2x daily loss budget used" breaker has no written definition (V2-7)
`check_strategy_breakers(daily_loss_budget_used=...)` suspends at >= 2.0 but nothing in the repo says what the
"daily loss budget" is. V2's `PaperContextProvider` therefore requires `daily_loss_budget_usd` (no default) and reports 0.0
if you give none, which leaves that one breaker input effectively off. Tell me the definition you intend
(e.g. N x per-trade risk, or a fixed USD amount).
**Answer:**

## E9 · The fallback strategies are NOT "already OOS-validated" (V2-3 / evidence gate)
`docs/v2/contracts.md` and the guide say `trend_follower`, `donchian_ensemble`, `tsmom`, `ctrend` are "already OOS-validated".
The repo's own gate (`docs/loop/results/P1-2026-09-27.md`) says: A1 donchian FAIL on deflated Sharpe (0.817 < 0.95) but
OOS Sharpe 0.77, +27%, max drawdown -9%, beats BTC hold and the basket; A2 CTREND-lite FAIL badly (OOS Sharpe -0.02,
-65%, -81% drawdown); A3 TSMOM FAIL (0.29 vs BTC 0.42, loses to costs). `trend_follower` has no gate result: "SMA50 beat
100/150/200 OOS" is a code comment. I added `GatedSource`: by default only `trend_follower` and `donchian_ensemble` may vote,
the other two count as UNAVAILABLE. Effect: a fallback with two voters caps at confidence 0.5, which is IGNORE, so during a
Jev outage the fallback is only logged, never escalated. The fallback still can never execute either way.
Decide: keep this default, enable all four, or replace the fallback strategies.
**Answer:**

## E10 · Kill controller thresholds are yours (V2-10)
`KillLimits` has no defaults: `exchange_failures_to_engage`, `max_feed_age_ms`, `max_orders_per_minute`,
`max_abs_slippage_bps`, `max_price_move_frac` (feed corruption), `daily_loss_cap_usd`, `min_request_confidence` for Hermes
halts. I will not guess them. Also: (a) should a Hermes halt need more than a confidence number (e.g. corroborating
evidence)? (b) is a rate limit on accepted halts wanted, so Hermes cannot starve trading?
**Answer:**

## E11 · Closing inventory: what is NOT built, and who must decide (V2 as of v2/live-runner)
**Built and tested:** contracts A-J; policy; Jev adapter + real TypeSafe client (verified live); deterministic fallback with the evidence
gate and real stances; risk engine (wraps the legacy layer); feature engine (no synthetic numbers, gap-safe); state builder; paper book;
kill controller + Hermes halt requests; whole-chain invariant test; public-data feed; paper runner (verified live).
**Needs YOU (not buildable by me):**
1. Deploy the runner as a resident process on the VPS (RAM ~2.4 GB free, supervision, restart policy): your approval.
2. ~~The numbers~~ APPROVED 2026-10-04 (E12). Still unknown: real fees/slippage (need real fills).
3. Question text of workflow v2 is my draft; then flip `active.json` (E4).
4. `src/trading/data/` is still missing from git (E2); it blocks 10 old test files, not V2.
5. Whether fallback exits may execute during a Jev outage (E5); whether to enable all four fallback strategies (E9).
6. Real money: nothing in V2 can place an order and nothing should until paper evidence exists. That is a separate, later decision.
**Deliberately NOT built:** a websocket feed (resident connection); Redis/Postgres (memory, per the guide); Jev decision-tree / conditional
questions (the Decision contract needs every signal); Hermes research/promotion contracts and a promotion-gate evaluator (nothing consumes them
yet); order/position event and execution-result contracts (the paper book logs its own events; they belong with a real execution engine);
the frontend (you said not yet); a rule on P(risk=HIGH|EXTREME) instead of argmax (needs a threshold from shadow data).
**Known limits:** runs measured in minutes, not days; paper results flatter reality by the unknown cost gap; trend stance is a reconstruction.
**Answer:**

## E12 · ANSWERED 2026-10-04: "approve all". Numbers for E7 / E10 / E8 are locked (V2-13, `v2/approved-limits`)
See `docs/v2/PROPOSED-LIMITS.md`: stop 3.5% / target 7%, daily loss cap $20 (2%), kill cap $40, kill price-move 3%, spread cap 5 bps, paper
spread 0.82 bps + 1 bp slippage, daily-loss budget $20. All derived from ~18,000 real 1-minute bars by a reproducible rule
(`python -m trading.v2.calibrate`). Real fees/slippage stay UNKNOWN until real fills exist.
**Answer:** approve all (2026-10-04). Locked in `src/trading/v2/approved.py`; paper only; real money and VPS deployment are NOT covered.


## E5 · CLOSED by decision under the owner's delegation (V2-15, 2026-10-04)
Fallback (now the System One gateway) may EXIT/REDUCE/TIGHTEN under the risk-reducing position policy (EXIT/REDUCE ≥ 0.70,
TIGHTEN ≥ 0.60), the same thresholds as Jev. The "never EXECUTE" cap still holds for ENTRIES. Reason: blocking exits trapped
positions whenever Jev was down; the cap exists to stop an unvetted source opening risk. See `docs/v2/V2-15-RESULTS.md`.

## E13 · Position-policy thresholds are unvalidated (V2-15)
EXIT/REDUCE at 0.70, TIGHTEN at 0.60 and TIGHTEN distance 1.5 x ATR(1h) are reasoned defaults, not measured. They cannot be
backtested for Jev, because that needs a paid call per historical cycle. The shadow log will show, per answer, what holding
would have done. Proposal: review after 30 positioned cycles.
**Answer:**

## E14 · Should an engaged kill switch still allow risk-reducing orders? (V2-15)
`contracts.md` says "kill switch engaged → REJECT" for every order, and V2 follows it. Consequence: while killed, advice
cannot EXIT. The resting stop and the L1 ratchet still protect (a stop amendment is not a new order). Option: let reduce-only
orders pass the kill switch. That is a risk-limit change, so I did not make it.
**Answer:**

## E15 · No measured entry edge (V2-15)
The gateway's entry model (features-v1, +7%/-3.5% in 14d) has no out-of-sample skill (AUC 0.49). Jev's entry skill is
unmeasured: it has said NO_TRADE in every live entry cycle. Exit management cannot create edge (pre-registered study: every
exit rule lost on test with naive entries). The next lever is the ENTRY signal: a different horizon/target, cross-sectional
features, or Jev's own shadow record once it says LONG. Owner's call on direction.
**Answer:**
