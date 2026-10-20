# The data-health report described a database nobody asked about

**Date:** 2026-10-20 (cron improvement pass)
**Files:** `src/trading/data/health.py`, `src/trading/data/store.py`
**Tests:** `tests/test_data_health_report.py` (8 new; 3 mutations verified),
`tests/test_data_health.py` + `tests/test_data_foundation.py` unchanged (8 old)
**Repair:** `reports/data-health.json` re-derived — `ok:false, 15×"no data"` →
`ok:true, ok_all:true, 60 symbol×tf groups, 9.56M candles, 0 duplicates`

⚠️ **`src/trading/data/` is gitignored** (`.gitignore:47` `data/`), so these
files have no git baseline and cannot be reverted with `git checkout`. That is
open task **T0.7** ("commit `src/trading/data/` — owner files"), escalated.
The original bytes were **not** preserved before the edit; the pre-fix
behaviour is reconstructed in `research/_health_prefix_snapshot.py` (marked as
a reconstruction) so the new tests can be re-run against it.
**T0.7 is now a risk with evidence, not a tidy-up.**

## What was wrong (three defects in 75 lines)

1. **The report was published by every caller.** `check_data_health(db_path)`
   always wrote `REPORTS_DIR/data-health.json`, even when `db_path` was a temp
   DB. The unit tests (`test_data_health.py`, 3 cases, each on a nearly-empty
   temp DB) therefore overwrote the live report every time they ran. The file
   on disk at 18:04 said:

       "ok": false,
       "problems": ["BTC/USDT:5m: no data", ... 15 entries]

   while the same function, run against the real store at 01:01, said
   `ok: true` with 9,563,044 candles across 60 symbol×timeframe groups. The
   18:04 report was **a test artefact about an empty temp DB**, and the daily
   heartbeat reads exactly this file. It was one test run away from paging the
   owner about data that was never missing.

2. **`storage_check()` could not fail.** It returned `{groups, duplicate_keys}`
   and no caller looked at it. A store that became unwritable, or flipped out
   of WAL, could not reach `problems` at all. The candle loop was the entire
   verdict.

3. **"no data" was never checked against the reader.** In WAL mode a fresh
   read connection can report nothing while the file is full of committed
   candles (the writer's uncommitted frame is invisible). A monitor that says
   "no data" must be able to distinguish "the store is empty" from "I cannot
   see the store".

## Fix

- `check_data_health(db_path, publish=False)` — **only the CLI publishes.**
  Callers and tests get the dict and write nothing.
- The report names its database (`"db": "/opt/trading-agent/data/trading.db"`).
  A report that cannot say what it looked at is not evidence.
- `ok` = candle verdict (unchanged semantics, so the heartbeat's exit code
  means what it always meant). `ok_all` = candles **and** storage.
  Both are reported; neither hides the other.
- `storage_check()` now returns `ok` + `problems` and actually probes:
  journal mode, WAL sidecar size, a write/rollback round trip, duplicate keys.
  It never raises — a health check that throws reports nothing.
  The legacy keys (`groups`, `duplicate_keys`) are preserved because
  `soak_ingest.py`, `ingest_public.py` and `test_data_foundation.py` read them.

## Tests (mutation-checked)

| mutation | test that fails |
|---|---|
| `if publish:` → `if True:` | `test_a_temp_db_run_does_not_overwrite_the_live_report` |
| `candles_ok = not problems` → `True` | 3 tests (stale, no-data, naming) |
| `info["ok"] = not problems` → `True` | `test_storage_can_fail_...`, `test_ok_all_...` |

Two of my own first drafts were wrong and the tests caught them:
- An `ok` flag set to `True` and never flipped passed **nothing** — the
  original `problems`-appending branches don't touch a flag. `ok` is now
  derived (`candles_ok = not problems`) so it cannot drift from the list.
- `chmod` cannot simulate a write failure: the suite runs as **root**. The
  read-only test uses `PRAGMA query_only=ON` on the connection instead.

## What this does NOT fix — the open question from 2026-10-19

`lesson-loop-main-crashed-2026-10-19.md` closed with: the `--quiet` path is
blind to failure, and *"that is the more important defect and it needs its own
decision (what should the cron say when a job returns rc≠0?)"*. **Still open
and still owner-facing.** The lesson wrapper was fixed to propagate rc; the
other `--quiet` jobs were not audited. Recorded here so the next pass does not
re-discover it.

## Standing rules added

> 1. A monitor must publish **only** from its entrypoint. A function that both
>    computes and writes a global artefact lets a unit test rewrite production
>    truth.
> 2. A verdict must name the input it judged (`"db": ...`). Without it, a
>    report about an empty temp DB is indistinguishable from a report about
>    the live store.
> 3. Every check must be able to fail. A `_check_*` whose return value nobody
>    reads is decoration, and "ok" computed from one of several checks is a
>    claim the code cannot support.
