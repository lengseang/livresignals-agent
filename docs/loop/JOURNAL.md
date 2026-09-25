# Loop journal

Newest first. Each entry: task, need check, what was done, evidence (commands and
results), and what was learned or blocked.

## 2026-09-25 · T0.2 tests/loop harness
- **Need check:** no external data or prod files; test-only code.
- **Done:** `tests/loop/conftest.py` with two autouse guards (no network unless
  `@pytest.mark.network`; importing any real-money module fails the test) and
  `test_loop_harness.py` (7 tests: interpreter is 3.12, `trading` importable, both guards fire).
- **Evidence:** `.venv/Scripts/python -m pytest tests/loop -q`: `7 passed in 0.07s`.
  Full suite with `--continue-on-collection-errors`: `1 failed, 53 passed, 3 skipped, 10 errors`;
  without this change: `1 failed, 46 passed, 3 skipped, 10 errors`. Same failure and errors,
  +7 passes. The existing suite is unaffected.
- **Learned:** a pre-existing failure, `test_forward.py::test_wallet_cap_no_dead_entries`,
  opens the real `data/trading.db`. Filed as T0.9 instead of bundling a fix.

## 2026-09-25 · T0.1 loop scaffolding
- **Need check:** no external data needed; facts come from the repo and this session's findings.
- **Done:** added LOOP (contract), ROADMAP, TASKS, ESCALATIONS, JOURNAL. Created the
  `loop/integration` branch from `origin/main` at `b0e8091`.
- **Evidence:** docs only; no code changed.
- **Known baseline:** CI is red on `main` (check-books hardcoded path; `trading.data` untracked).
  The loop's own tests go in `tests/loop/`.
