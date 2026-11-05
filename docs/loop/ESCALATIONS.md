# Escalations: waiting on the owner

Newest first. The owner answers inline under **Answer:**; the next iteration picks it up.

## E9 · `ETH_TURTLE_H4` re-acquired the ETH position the 10-31 fix released (2026-11-01)
**RESOLVED 2026-11-01 by `2ac046e` — no owner decision needed. Answer below.**
**VERIFIED 2026-11-02 on real data** (the check the answer below said was owed):
the first NEW-bar passes after the fix — 2026-11-01T20:07Z (20:00 bar) and
2026-11-02T00:07Z (00:00 bar) — both log `symbol_owned_by_other_strategy` AND
still evaluate the treatment (`action none`); both arms are at bar 11-02T00:00
with the treatment flat and the control holding. Demo exchange: ONE 1.2 ETH,
avg 2241.49, stop 2192.37. The lag is closed. A separate bookkeeping residue
from that repair (a hand-written `released_unowned_position` field) was removed
2026-11-02 — see the JOURNAL entry of that date.
`state/crypto-trend-demo/state.json` now shows TWO arms holding ETHUSDT:
`ETH_TURTLE_H4` `in_pos:true, qty 1.2, entry 2241.49, ledger_id:null` (no
ownership flag) and `RANDOM_ENTRY_CONTROL` `in_pos:true, qty 1.2,
ledger_id:ea-RANDOM_ENTRY_CONTROL-1793390400000, owns_exchange_position:true`.
The 10-31 pass released exactly this claim; it was back by 2026-11-01T00:07Z and
has logged `hold` every 4h since (12:07Z last, `dry:false`). `trades/loop/
test_demo_runner_dry_and_exit.py::test_live_state_has_no_unowned_double_claim`
is red on it (821 passed / 1 failed on the full suite).
I did not repair it: it is a live demo/exchange state file, and the fix is
either (a) re-run whatever released it on 10-31, or (b) allow a paired
treatment/control to co-hold (the guard already exempts pairs for *adoption*,
but the state itself then carries two 1.2-ETH claims on one position). Which?
Note both arms hold the same qty and stop, so this is one physical position
claimed twice — if anything ever closes from `ETH_TURTLE_H4`'s path the P&L
books to the wrong arm.
**Needed:** the intended invariant — one owner per position with the treatment
adopting *without* a claim, or paired arms allowed to co-hold?
**Answer:** Neither — the premise offered a false choice. (b) is not available:
the exchange holds ONE 1.2 ETH position, so two claims cannot both be true, and
(a) alone would just be re-run again. **The real cause was the 10-31 fix
itself.** It exempted paired arms from the ownership guard, which correctly
stopped the control from disarming its treatment, and in doing so also let the
FLAT treatment ADOPT the control's position (`ledger_id: null`) with nothing to
release it. Corrected timeline: the re-adoption was **2026-10-31T13:07Z**, not
11-01T00:07Z — the 00:07 line is the first `hold` after a midnight gap, and
every `hold` just re-logged the claim; no new adoption event ever fired.
Invariant adopted: **two arms may both be enabled on a symbol, but only one may
claim the exchange's single position.** Ownership now governs CLAIM only;
`decide()` is still never blocked, and a phantom claim is released (never sold)
when another arm can prove ownership.
Shipped as `2ac046e`: red-first tests in
`tests/loop/test_paired_claim_single_owner.py` (4 of 7 fail on HEAD), the two
old tests that had encoded the double claim as the contract rewritten, live
state repaired, and **verified by replaying the real corrupt state through the
shipped `run_once()`** — which is how the third defect was found (the repair sat
behind the `no_new_bar` continue, so an arm that had already seen the bar could
never be repaired; the tests were green on that version). Full suite
**830 passed / 3 skipped / 0 failed**.

## E8 · What schedules `run_crypto_trend_demo.py`? (2026-10-31)
**ANSWERED 2026-11-01 (found, not needed from owner).**
The runner takes a pass every ~30 min (decisions.jsonl: 12:07, 12:37, 13:02 UTC) and
places real Bybit **demo** orders, but its path appears in no crontab I can read
(`crontab -l` for ubuntu and root both lack it) and in no pm2 process. I will not
guess: the answer decides whether the ownership fix is live yet, and separately
whether the schedule is intended.
**Needed:** where it is scheduled (cron file, systemd timer, or another host), so the
two-bar ETH_TURTLE_H4 lag can be confirmed closed on the next real pass.
**Answer:** It is scheduled by the **Hermes cron subsystem**, not cron/systemd/pm2 —
which is why `crontab -l` and `pm2 list` both miss it:

```
~/hermes-home/cron/jobs.json
  id bdd39cc7f4a7  "crypto-trend-demo-runner"  expr "7,37 * * * *"
  script crypto_trend_demo.sh  (→ scripts/run_crypto_trend_demo.py)  active, last_status ok
```

So "check the crontab" is an incomplete question on this host — a job can be live
in three schedulers. **The ownership fix IS live**: the 2026-11-01T18:07Z pass ran
this code and the treatment did not re-adopt (state md5 changed, ETH_TURTLE_H4
stayed flat, RANDOM_ENTRY_CONTROL kept the position). On lag: at 18:07 the
treatment has an H4 bar already consumed, so it takes the `no_new_bar` short
circuit and writes no decision line at all — **absence from the log is the
healthy signal here, and it is also why a freeze looks identical to health**.
The lag can only be confirmed closed on a pass where a NEW H4 bar exists
(00:07/04:07/08:07/12:07/16:07/20:07 UTC); check `last_bar` against the control
then rather than inferring it from silence.
Note the cadence is every 30 min while the strategies are H1/H4; the extra passes
are cheap (each is a `no_new_bar` no-op) and not a defect.

## E7 · Pause `futures_executor` until its signal is re-tested net of fees? (T1.5, PR #13)
It trades **3x real futures** (every 10 min in `crontab.backup`, no enable flag) on the 15m SHARP + EMA200 signal. The
only evidence is a gross study (its scripts have no fee handling): "SOL WR 51% → 61%, OOS +13.5%". At 3x, fees are
charged on 3x the margin. Recommendation: pause its cron line (your action) until the T3.3 gate re-tests it with fees.
Also: which crontab is live? (`crontab -l`, E1)
**Answer:**

## E10 · Reader fix for the "REAL $" row (T0.10, measured 2026-11-05)
Two readers report real money from the wrong record. `expectancy_vs_fee.py` prints
`trend_follower (REAL $)` from `trend-state.json` → `log` (a 53-symbol PAPER log);
`fee_audit.py` (09:50 daily) does the same and multiplies it by a hardcoded $5 stake.
The real record is `real.closed`: n=18, `pnl_usd` null on all 18, mean +6.19% but
**+0.09% (t=+0.05) once one pre-existing NEAR record is removed**, and fees actually
paid ≈0.07% RT, not 0.1594%.
Proposal + test plan: `docs/proposals/real-book-reported-on-paper-log-2026-11-05.md`.
Q: **yes/no to editing these two readers in one PR (output wording + which key is read;
no trading logic, no state writes)?** Until answered, the report keeps printing the
paper log under a REAL label and I keep annotating it in shift reports.
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
