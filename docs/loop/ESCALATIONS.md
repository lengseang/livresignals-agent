# Escalations: waiting on the owner

Newest first. The owner answers inline under **Answer:**; the next iteration picks it up.

## E8 · Operator terminal decisions (T6.1, PR #21 §11)
(1) Should `/terminal` exist on the public deploy, or only in local dev? (The design keeps it inert without a feed URL.)
(2) Approve the new `term-*` colour token namespace (the front end's AGENTS.md requires approval for token changes).
(3) Hide the animated background on `/terminal` with one scoped CSS rule, or use separate root layouts?
(4) Keep numbers in Inter tabular figures (the site rule is "no monospace for money"), or go all-mono in the terminal?
(5) Checks + screenshots only, or add a dev-only JS test runner (a new dependency)?
(6) May T6.2 add the terminal's naming and feed-validation rules to the front end's AGENTS.md?
(7) Widen `check-tokens.py`'s glob yourself, or rely on the flat `components/Term*.tsx` naming?
(8) The profit/loss colour contrast measures 1.50 by WCAG vs 1.19 in the config comment. Which method was intended?
**Answer:**

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
