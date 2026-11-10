# Escalations: waiting on the owner

Newest first. The owner answers inline under **Answer:**; the next iteration picks it up.

## E13 · The V2 paper lanes died on 2026-11-05T09:01Z and 40h later every watcher still says nothing (2026-11-07)
Both lanes (`paper`, `paper-b`) have written no heartbeat since
**2026-11-05T09:00:03Z** (cycles 1728, steps 8639, then stopped). `pm2` has been
restart-looping both runners ever since; the 427th consecutive line in
`v2-paper.out.log` is `PREFLIGHT FAILED`:

```text
FAIL jev_key_authenticates  HTTP 403 (key rejected: revoked, rotated or mistyped)
```

Every other preflight check passes (`paper_only: PASS`, no order path). So this
is **not** a money path and **not** a trading result — it is a dead credential
plus a delivery chain that hid it:

1. `run-v2.sh` sends a Telegram notice on preflight failure and sleeps 300 s.
   No `notify_state.json` exists in either lane dir → that send failed and
   `|| true` dropped it. Nothing else looks at `preflight.json`.
2. `watchdog.py`'s `runner_silent` rule is correct (mtime-based) **and runs
   inside the runner's own process**, so it dies with the runner it watches.
3. `scripts/v2_gate_watchdog.py` *does* see it and exits 1 —
4. but `~/hermes-home/scripts/v2_gate_roll.sh` ends both branch paths in
   `exit 0`. `eb407360fa04` (v2-scale-gate-check) = **4 completed, 0 failed**.
   Only `d071de6fa4a4` (v2-top8-gate-watchdog, keeps its rc) delivered, 4×.
5. `watchdog.digest()` prints `jev 1575/1725` = 91.3% turnout — **read from the
   frozen heartbeat**. A ratio of two frozen counters is a frozen ratio.

I did **not** restart anything, did not touch `config/v2.env`, pm2 or
`V2_SYMBOLS`. Full write-up: `docs/proposals/v2-lane-silent-since-2026-11-05.md`;
read-only reproduction `research/_v2_lane_silence_probe.py`; 6 tests in
`tests/loop/test_v2_lane_silence.py`.

**Needed:** (b) a working `TYPESAFE_API_KEY` (or its rotation) in
`/opt/trading-agent-v2/config/v2.env` + a restart of both lanes — only you have
the key; and (c) yes/no to preserve the gate's rc in `v2_gate_roll.sh` and add
one line to the stale branch that prints `preflight.json`'s failing check, so
the alarm says *why* next time.
**Answer:**

## E15 · The ensemble paper lane has been dead 6.4 days by construction — re-size the target, or retire it? (T0.19, 2026-11-10)

The strategy-book ensemble lane (`ensemble_paper.py`, every 5 min, paper only,
no money at risk) has **not opened a position since 2026-11-05T05:00:53Z**. It
is not broken and it is not a quiet market: it refuses every candidate on
geometry, correctly, forever.

```text
book         205 closed, 0 open, net -8.3386 after 0.28% RT
             18.5% WR, 38W/167L, exits: TP 0 / SL 57 / TIME 148
lane         last open 2026-11-05T05:00:53Z  -> 6.4 days / ~1,540 runs
refusals     ens|momentum|5m   target +3.0% vs CEILING 0.5%
             ens|momentum|30m  target +3.0% vs CEILING 1.5%
             (ceiling = the widest target reachable >=5% of the time)
P(reach)     BTC 30m +3% in 12 bars = 1.71%  (reproduced: 4,868 bars)
             BTC 5m  +3% in 48 bars = 0.4-1.1% (window-dependent)
             floor is 5% — every window and both TFs say no
```

⚠️ The source comment's `5m = 1.4%` did **not** reproduce: re-measured today with
the module's own `_reach_freqs`, 5m gives 1.05% (3k bars), 0.40% (8k), 0.94%
(20k). The 30m 1.71% does reproduce exactly. The verdict is unaffected — a
4-12× margin below the floor — but the 5m number in the code comment is stated
too precisely for a quantity that swings 2.6× with window length.

Every ensemble sizes its target as `2% * rr`; `rr` is a strategy-book parameter
and **no ensemble carries it as a knob**, so a refused geometry is refused on
every future run. The gate (`MIN_P_MARGIN = 5%`) is doing exactly its job — it
is the *targets* that were never reachable.

This shift did **not** touch the targets, the floor, or `rr`. It only made the
death visible (see TASK T0.19) and fixed the card, whose first version claimed
the last open was *today*. The card now reads:

```text
NO OPEN IN 59 RUNS (last open 2026-11-05T04:50:00Z (5.6d))
  cause: REFUSED on geometry (ens|momentum|5m) — unless the writer changed;
         the target a reachable hold allows is an owner call
  the 205 closed rows are the LAST record this book made
```

**Decision needed — three options, in my order of preference:**

1. **Retire the lane (my recommendation).** TP 0 in 205 closes at 18.5% WR says
   the geometry never worked; the 5m/30m momentum families are the ones the
   strategy book's own gate now rejects. Stopping the cron is honest and saves
   a log that grows 5,181 refusal lines. Reversible.
2. **Re-size to a reachable target** (e.g. cap target at the measured ceiling,
   ~0.5–1.7%). ⚠️ This is **a new strategy, not a repair** — the forward record
   would restart from zero, and a target chosen from the same measurement that
   refuses it is an in-sample fit. If we do this, it needs a pre-declared bar
   (e.g. ≥100 closes, TP > 0 at all) written down *before* the first trade.
3. **Leave it refusing** and read it as a closed experiment. Costs nothing but
   the log noise (which T0.19 now suppresses to one line per change).

There is no option where it starts opening at +3% — the horizon cannot reach it.

**Answer:**

## E14 · Real money can only open 15 of the 53 coins the trend strategy studies (T0.15, 2026-11-07)
`trend_follower.py` runs `TREND_REAL=1` every 4h. `SYMS` was expanded to 53
coins on 2026-09-17; `REAL_SYMS` was not touched and still holds 15. The entry
loop filters candidates through `REAL_SYMS`, so a run that goes long only
outside that set places nothing — and until this shift said nothing either:

```text
2026-11-07 12:10  long 3/53: [INJ, ONE, PEPE]    real positions: 0/3
2026-11-07 16:10  identical
2026-11-05 → 11-07 in logs/trend.log: 0 REALBUY, 0 ERR, 0 "entry blocked"
```

The silence is fixed (T0.15, review): the card now carries a `Did not act`
block naming the declined coins and the run line appends `paper-only N`.
**Whether to widen `REAL_SYMS` is yours, not the loop's** — widening it puts
real money on coins that never passed `promotion_report.py`'s bar (>=55% WR
over >=15 paper trades). Options I can execute on your word:

(a) leave it at 15 (nothing changes but the reporting);
(b) promote the specific coins that HAVE passed the bar (the report names them);
(c) widen to the full 53 (I would advise against — current breadth is 0up/8down
    and the paper book is net-negative after fees).

Also relevant and unresolved, not actionable by me: the spot wallet holds
`BNB 0.0004, XRP 0.0706, ADA 0.0716, SUI 0.1` and **no USDT**, so
`_real_can_trade()` (`free >= 6.0`) refuses every real ELITE entry regardless of
`REAL_SYMS`. If you want real trading at all, USDT has to sit in Spot (not
Funding).

**Answer:** (a) / (b) / (c) —

## E12 · Should the strategy lab keep running 96×/day? (T1.6, 2026-11-06)
T1.6 measured the lab read-only (`src/trading/ops/lab_repetition.py`). It fires
every 15 min, scores 3 symbols × 3 entry × 2 exit × 3 rr = 54 points on **1h**
bars, and logs one `rejected.jsonl` line per point — 96 runs and ~5,184
evaluations a day, forever. On 3,274 runs / 166,119 rejects:

```text
grid points that EVER cleared the split gate   8 of 54 — ALL SOL
BTC + ETH (66.7% of the grid)                  0 ever advanced
runs with a survivor                           1,538
  … every survivor died at boundary gate       1,538  (100.0%)
last NEW grid point                            2026-10-22T09:15
rejected.jsonl                                 27.9 MB, +0.82 MB/day
```

The gate is not "finding nothing" — it re-derives the same eight refusals 96
times a day. Two of the three symbols have never produced a single point that
survived the split, so two thirds of every run is spent re-testing coins that
have never advanced.

**This is not a trading question and nothing is broken** — it is a spend
question. I have deliberately changed no schedule, no grid and no parameter.
Options, cheapest first: (a) leave it (cost is disk only, ~24 MB/month);
(b) drop to hourly (6× less, ~same information); (c) rebalance the grid toward
SOL or change the universe, which is a research decision, not a cron one;
(d) pause it. My read: **(b)** — the measured cadence buys nothing at 15 min on
1h bars, and the 8-of-54 ceiling is a *gate* question, not a cadence one.

**Needed:** which option, and whether the grid/universe should change at all.
**Answer:**

## E11 · Two quiet cron wrappers still lose a failure (T0.12, 2026-11-06)
T0.12 audited every scheduled job. `~/hermes-home/cron/executions.db` proves a
non-zero exit **is** delivered (`d071de6fa4a4` failed twice on 2026-11-05 and the
full gate text is in the record), so the only way a quiet job hides a crash is
a wrapper that forces `exit 0`. **Two still do**, both with the exact
pre-2026-10-19 template:

```
OUT="$(... scan_ping.py 2>&1)"
if [ -n "$OUT" ]; then printf '[SCAN] %s\n' "$OUT"; fi
exit 0                                  # <- rc never read
```

1. **`run_scan_ping.sh`** — the STALL ALERT monitor, 48×/day. If it raises, the
   monitor that is supposed to notice a dead bot is itself dead, and cron records
   `completed` + delivers the traceback as though it were a normal scan line.
   No failure yet in 88 attempts; this is latent, not active.
2. **`run_trading_digest.sh`** — 1×/day, same shape.
3. Related, same family: **`run_strategy_book.sh`** checks `strategy_book.py`'s
   rc but **not** `strategy_compact.py`'s (up to 10 min), then ends `exit 0`.

Fix is 3 lines per file and changes no message wording: capture `RC=$?`, print
the trace only on failure, `exit "$RC"`. I did **not** apply it — these are the
job wrappers that deliver to the owner, and T0.12 says the rc-to-message wording
is yours. The template is proven in `tests/loop/test_quiet_cron_exit.py`.

**Needed:** yes/no to apply the rc-preserving template to those three wrappers?
On yes, failures start arriving as a `⚠️ … exited N` line plus the scheduler's
`exited with code N`; no-op runs stay exactly as silent as today.
**Answer:**

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
