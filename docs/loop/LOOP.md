# Autonomous development loop: operating contract

This file is the contract for unattended development on this repo. Every loop
iteration reads it first. The owner watches; the loop plays developer, manager,
reviewer and operator, inside the limits below.

Companion files:
- `ROADMAP.md`: phases and exit criteria (the why)
- `TASKS.md`: the task board (the what, and its status)
- `JOURNAL.md`: one entry per iteration (what happened, with evidence)
- `ESCALATIONS.md`: things only the owner can decide or provide
- `designs/`: one design note per task that changes behaviour

## 1. One iteration

1. **Orient.** Read `TASKS.md`, the last 3 `JOURNAL.md` entries and `ESCALATIONS.md`.
   Check whether the owner answered an escalation; if so, unblock that task.
2. **Pick.** Take the first task in `TASKS.md` whose status is `todo` and whose
   dependencies are `done`. Mark it `doing`. If nothing is pickable, write a
   journal entry saying so and stop.
3. **Need check.** Before any work, answer in the journal:
   - Do I need **data** (prices, fills, ledger, logs)? From where?
   - Do I need **content or facts** (fee schedules, API limits, docs, papers)?
     If yes, **search the web first** and cite the sources. Never answer from memory
     anything that can go out of date: versions, fees, rate limits, endpoints.
   - Does it need **production files**? Then write an escalation listing the exact
     files and stop the task. There is no SSH; the owner supplies read-only copies.
4. **Design** (if the task changes behaviour). Write `designs/<task-id>.md`:
   problem, options, choice, failure modes, test plan. Then run an adversarial
   self-review ("it is 3 months later and this failed: why?") and record it in the design.
5. **Build** on branch `loop/<task-id>-<slug>`. Cut it from the tip of the most recent
   unmerged loop branch (a stack), or from `loop/integration` if nothing is pending.
6. **Verify.** Run the real checks: pytest on the new tests with `.venv` (CPython 3.12.3),
   `python -m compileall` on the touched dirs, and a real runtime check if it runs.
   Paste the command and its result into the PR body.
7. **Ship.** One task = one PR, with the reason in the commit message. The PR's base is
   the branch it was cut from, so each PR shows only its own diff. **The loop never
   merges anything; the owner reviews and merges.** Record the PR link in `TASKS.md`
   as `review (#N)`. A task whose deps are in `review` counts as satisfied for picking,
   because it builds on the stacked branch.
8. **Record.** Mark the task `done` (or `blocked` with the reason), add a
   `JOURNAL.md` entry, and add any new tasks you discovered to `TASKS.md`.
9. **Reflect.** If you learned something durable (a pitfall, an API quirk, a
   disproved idea), write it into the relevant doc. Negative results count as results.

## 2. Autonomy boundary

**The loop may do these on its own:**
- web research, reading code, reading public docs and public market data
- writing docs, designs and research notes
- adding **new** modules for paper-only features (no import of any live/order client)
- adding tests, fixtures, and local data recordings under `data/local/` (gitignored)
- running local services on this machine for testing
- opening PRs (stacked, see §1 step 7); never merging them
- in `livresignals` (front end): branch work for the operator terminal, on a
  `loop/*` branch only, from Phase 6 on

**The loop must escalate these and never do them itself:**
- anything on the owner's no-touch list: `config/*.env`, ledger JSON/.jsonl,
  `check-*.py` and prebuild guards, the crontab, any code that places or manages an order
- modifying existing real-money scripts: `trend_follower.py`, `live_executor.py`,
  `futures_executor.py`, `unified_trader.py`, `manager.py`, `src/trading/exchange/live_client.py`
- running `live_executor.py`, `trend_follower.py`, `night_tuner.py`, `watchdog_alert.py`
- merging any PR (all merges are the owner's review step), deploying to the VPS,
  touching pm2 or cron
- sending a real Telegram message or posting publicly (dry-run until approved)
- signing up for services, using or creating API keys, spending money
- anything illegal, including scraping paid or private signal groups; public sources only

## 3. Standing rules (from the owner)

1. **Design first** for trading logic, risk and the ledger.
2. **Never write a synthetic number.** If a value is unknown, record `UNKNOWN` and
   say so. A missing value is honest; a plausible wrong one is not.
3. **One change per PR**, with the reason in the commit message.
4. **Research rules** (from `SYSTEM_MAP.md` §4). Every strategy claim must be based on:
   a shared-account simulation, an IS/OOS split, fees and slippage included, a
   comparison to BTC buy-and-hold, and a statement of the test period's regime.
   When many variants are tested, apply a multiple-testing correction
   (deflated Sharpe or similar). A high win rate alone is never evidence.
5. **Paper is labelled.** Every signal and number produced by this loop is paper or
   simulated and must be marked as such anywhere it is displayed or broadcast.

## 4. Environment facts

- Interpreter: `.venv` = CPython 3.12.3 via uv (matches prod 3.12.3). Never 3.14.
- `uvloop` is excluded on Windows (install from a filtered requirements copy).
- **Baseline CI is red for reasons that predate the loop:** `check-books.py`
  hardcodes `/opt/trading-agent`, and `src/trading/data/` is missing from git
  (swallowed by the `data/` gitignore rule), so 10 of 16 test files fail to
  import. New loop code must be testable without `trading.data`. Its tests live in
  `tests/loop/` and must pass on their own.
- Front end: `D:\1111-Works\LIVRsignals\livresignals` (Next.js). Local `main` is
  behind `origin/main`; cut loop branches from `origin/main`.

## 5. Stop conditions

Stop the loop and write an escalation if any of these happens: the same task fails
verification in 2 iterations running; a change would need an item from the
escalate list; a result contradicts `SYSTEM_MAP.md` §5 in a way that affects real
money; or anything looks like a live-trading side effect.
