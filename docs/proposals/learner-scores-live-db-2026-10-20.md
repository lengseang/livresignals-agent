# The learner's verdict was about a market that ended 65 days ago

**Date:** 2026-10-20 (overnight shift)
**Status:** FIXED — the learner now scores the live candle store
**Component:** `scripts/paper_learner.py` (cron `0 10 * * *`, daily)
**Supersedes:** `learner-scored-frozen-data-2026-10-20.md` §6 option 1

---

## 1. What this closes

The 09:30 run on 2026-10-20 shipped **provenance only**: the learner still
scored `data/live_fine.json` + `data/alts_fine.json` (both frozen 2026-08-15)
and merely printed `INPUT: … last updated 65d ago`. Printing the age makes a
defect visible; it does not remove it. Today's shift did the actual fix
(option **1** of that document's §6).

## 2. The change

`_load_all_series()` now reads `data/trading.db` first, for every key in
`SERIES_KEYS`. The sweep files are consulted **only** for keys the DB cannot
serve, and each such key is stamped `frozen <N>d ago` in the `INPUT:` line.

Four keys exist only in the sweep files and are therefore still frozen-sourced:

```
5m:BTC/USDT   15m:BTC/USDT   30m:BTC/USDT   30m:XRP/USDT
```

The DB cannot serve them because its BTC/ETH/SOL 5m/30m series stop at
2026-08-15 (`5m` gap of 1105 bars; `30m` gap of 18 bars) and XRP has no 30m
row before 2026-01-01 in the frozen file's window either. **This is a data gap,
not a code gap** — it is reported, not papered over.

Verified the migration is like-for-like: all 15m bars in the frozen files are
byte-identical in the DB (1 mismatch of 126,936 = the last bar, which became a
closed candle in the DB; 1 of 126,947 for SOL). So switching sources does not
silently change the series definition.

## 3. Why provenance alone was inert — the cache

`best_params()` reuses a persisted `scored` grid from
`data/paper-learner-state.json` when `cost_rt` matches. That cache was built
from the frozen files. Without a new guard the learner would have printed the
**old** verdict forever against a **new** source — the exact "stale INPUT"
failure the 09:30 fix was written to expose, one layer deeper.

Added an `input_sig`: a sha256 over each scored series' `name:len:last_close:
last_ts`. Any source change — or simply a new bar — invalidates the cache.

```
first run after the fix:
paper-learner: cached grid was scored on DIFFERENT data
  (cost_rt=0.001594, input_sig None != 8e9b879a4fec) — recomputing
```

Cost: one full 72-combo grid, ~6 minutes. Thereafter daily.

## 4. The verdict changed — and so did the truth

```
             source            config            acc    net_ev       n
before   frozen files      -0.08 / 30 / 4       56.3%   +0.184%      907
after    trading.db        -0.08 / 30 / 4       59.1%   +0.481%     2645
```

More trades, higher EV, higher accuracy — the **same** config, on two extra
months of bars plus the full 2017-onward history the frozen files truncated at
2023-01-01.

And it is still untestable:

```
RECENCY: same config fired ZERO times in the last 90 days
```

Re-derived independently during this shift (rule 9, not quoted):

```
config          sym        90d_n   full_n   ev%_90d
(-0.08,30,4)    BTC/USDT       0      406      n/a
(-0.08,30,4)    ETH/USDT       0      756      n/a
(-0.08,30,4)    SOL/USDT       1      886      n/a
```

Split-half on the DB confirms the edge is real but crash-concentrated, and
that the OOS half is thinner than the IS half for every symbol:

```
             IS n    IS ev%     OOS n   OOS ev%
BTC/USDT      335    +0.378        71    +0.671
ETH/USDT      575    +0.639       181    +0.392
SOL/USDT      658    +0.511       228    +0.069
```

## 5. The new guard: recency

`main()` now prints a `RECENCY:` line for the config it just selected, and
**refuses to adopt a winner that fired fewer than 20 times in the last 90
days**, recording it under `untestable_best` instead:

```
paper-learner: kept current (best config is untestable — it fired 0 times in
90 days (full-sample EV +0.481% describes a crash-heavy past, not the live
regime); not adopted)
```

This is the 09:30 document's §7 "second-order finding" made mechanical. That
section asked for `edge_decay` suggestions to be gated on the edge having
actually traded in the judged window; the same denominator error applied to
adoption itself, and now does not.

**The honest statement of state:** the learner's best measured rule is
net-positive on a full sample that includes several crashes, and cannot be
validated by a daily learner in the current regime because it barely trades.
It remains a SIGNAL — no trader reads `paper-learner-state.json`.

## 6. Tests

`tests/test_paper_learner_provenance.py` (7 → 9 tests), all mutation-checked:

* `test_db_is_preferred_over_a_frozen_sweep_file` — DB bars win; window is
  the DB's window.
* `test_sweep_file_still_serves_keys_the_db_cannot` — no silent shrinking of
  the grid.
* `test_best_params_scores_fresh_bars_a_frozen_file_would_hide` — a crash that
  exists only in the DB produces trades; a frozen flat file produces none.
* `test_untestable_winner_is_not_adopted` — the real 2026-10-20 winner is
  recorded, not adopted.
* `test_a_config_that_trades_recently_IS_adopted` — the control: the gate can
  still PASS (rule 10 corollary — a check that cannot pass is not a check).
* `test_the_recency_counter_matches_the_grid_on_real_bars` — the RECENCY count
  and `simulate` cannot drift apart.

Stubbing `_db_series` to return nothing fails 3 of these; restoring passes.
The pre-existing fixtures in `test_paper_learner_resilience.py` and
`test_paper_learner_net.py` were also pointed at a non-existent DB — otherwise
the live store leaked into fixtures asserting "no data" (they were reading
1M rows from `/opt/trading-agent/data/trading.db`).

Full suite: **698 passed, 3 skipped**. Replay engine self-test: all rules hold.

## 7. What is still open

1. **The 4 frozen-only keys.** Backfill BTC/ETH/SOL 5m+30m and XRP 30m into
   the DB and the sweep files can be retired entirely. Until then the `INPUT:`
   line will keep naming them.
2. **`ingest_all.py` cannot re-close those gaps.** It fetches `limit=200`
   bars per run and upserts forward, so a 1105-bar hole is invisible to it —
   `detect_gaps()` only inspects the 200 rows just fetched. A gap this old
   needs a backfill script, not the live ingester.
3. **`check_data_health()` cannot see a mid-history gap.** It compares
   `MAX(close_time)+1` to `MAX(open_time)` — the newest bar only. A 1105-bar
   hole in August reports `ok: true`, and did, for two months.
