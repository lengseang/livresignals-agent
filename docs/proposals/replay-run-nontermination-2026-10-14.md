# Learning: ReplayEngine.run() never terminated when the window reached the end of the data

**Date:** 2026-10-14
**Cron class:** LEARNING
**Commit:** `replay: run() must terminate on exhaustion, not just on now > end_ms`

## What was found

The full test suite **hung**. `tests/loop/test_hunt5_gate.py` (added 2026-10-13,
but left **untracked** — so it never ran in CI) collects 4 tests and then stalls
on the first one forever; `pytest` never returns and the suite has to be killed.
An untracked test is a test that runs only when someone happens to be in that
directory, which is how a hard hang shipped unnoticed.

The hang is not in the test. It is in `scripts/replay_engine.py`:

```python
def run(self, decide, on_close=None):
    while True:
        self._fill_pending()
        now = self._aligned_now()
        if now > self.end_ms:        # STRICT greater-than
            break
        ...
        if not self.advance_all():   # returned True UNCONDITIONALLY
            break
```

`advance_all()` returned `True` even when every feed sat on its last bar and no
cursor could move. The master clock is the **minimum** started cursor, which
stops dead at the last bar's `close_time`. So whenever the requested `end_ms` is
`>=` that last close — i.e. the window reaches or overshoots the end of the data
— `now <= end_ms` stays true forever, nothing advances, and `run()` spins until
the process is killed. A replay engine that can loop forever on a legal window is
a denial-of-service on every caller, and here it took out the whole test suite.

## Why it had never fired

`hunt5` sweeps a window ending **2026-10-01**, and the DB holds bars after that,
so `now` overtakes `end_ms` and the loop exits the "normal" way. The first caller
whose window ends *at the data end* is the hunt5 split-half fixture, whose `END`
is exactly one bar past its last close. The bug needed a caller the production
path never had; the test was that caller.

## The fix

`advance_all()` now reports whether the clock actually moved (`moved`), and
returns `False` when it cannot — exhaustion is termination, and `run()` already
breaks on `False`. `test_replay_termination.py` pins it:

- a window ending at, +1, +2 and +10 bars past the last close now terminates;
- a window inside the data still terminates (regression guard);
- `advance_all()` returns `False` and does **not** move the cursor once drained.

Mutation check: restoring `return True` makes
`test_run_terminates_when_the_window_reaches_the_end_of_data[1]` hang (timeout
kills it, no output); with the fix it passes in 0.05s.

## Evidence

- Before: `pytest tests/loop/test_hunt5_gate.py` → collects 4, **never returns**
  (killed at 60s). `tests/loop/` could not complete.
- After: `pytest tests/loop/` → **351 passed, 3 skipped in 39.8s**, including the
  4 hunt5 gate tests. Full suite `pytest tests/` → **596 passed, 3 skipped**.
- Production behaviour unchanged: the split-half PASS candidate
  `volbreak 20 x2.0` still passes both halves (IS net +11.02 PF 1.14 /
  OOS net +15.77 PF 1.19). The new branch of `advance_all()` is unreachable in
  the sweep because its window ends before the data does.

## The generalisable lesson

**A loop whose only exit is a strict inequality has a boundary it never exits
through — assert the progress condition, not just the clock condition.** Two
habits fall out of this:

1. A dependency's return value is a contract. `advance_all()` returned `True`
   that meant "I did not crash", while its caller read it as "I advanced".
   When a loop trusts a boolean, the boolean must mean exactly the thing the loop
   is trusting it for — here, **progress**.
2. An untracked test is not a test. `test_hunt5_gate.py` was written, was run
   once by hand, and was never committed; the suite that CI runs never saw it, so
   the hang it exposes was invisible for a day. Commit new tests with the change
   that motivates them, or the next person's "the suite is green" is a statement
   about a suite that does not include them.
