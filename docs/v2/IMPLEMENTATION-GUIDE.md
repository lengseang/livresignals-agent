# V2 Implementation Handoff — Guide for Claude Opus

> **Audience:** Claude Opus (or any capable coding agent) working on a local machine,
> implementing V2 from `docs/v2/contracts.md`.
>
> **Status:** The contracts are DESIGN. Nothing in V2 is implemented. The running system
> is trading live with real money on a small account and must not be disturbed.

---

## 0. Read this first (5 minutes, saves hours)

You are implementing against a **live production trading system** that places real orders.

```
Repo      : the trading repo (branches: main = deployed)
Host      : a Hong Kong VPS, 154.219.105.99, Ubuntu 24.04, 3.9 GB RAM
Tests     : 482 passed / 3 skipped / 0 failed  ← this number must not regress
Python    : 3.12, venv at ./venv
```

**Three facts that will bite you if you miss them:**

1. **There is a 60-day forward paper clock running** (`livr-shadow` service). Restarting
   it voids the clock and destroys six weeks of accumulated evidence. **Never touch it.**

2. **This box has ~2.4 GB available RAM** and already runs: `livr-shadow` (~946 MB),
   `livr-recorder` (~143 MB, holds the only un-refetchable data), a feed, Caddy,
   the gateway, and 40+ cron jobs. **Do not add resident processes without saying so
   explicitly.** The architecture note names Redis and Postgres — treat that as an open
   question, not an instruction.

3. **`exit 0` is not success.** This system's history includes an 8-hour outage where
   every cron job exited 0 while doing nothing, because a log directory was missing.
   Verify outcomes, never exit codes.

---

## 1. What to read, in order

| File | What it gives you |
|---|---|
| `docs/v2/contracts.md` | **The spec. This is your primary source.** Nine contracts A–I |
| `docs/adaptive-framework.md` | Existing architecture and the house reasoning style |
| `AGENTS.md` | Repo rules — read this before writing any code |
| `scripts/unified_trader.py` | Today's decision path (what V2 replaces) |
| `scripts/live_executor.py` | Live execution — **read-only unless told otherwise** |
| `scripts/futures_executor.py` | Futures path, holds the hedge-mode quirk |
| `scripts/risk*` / config | Existing risk limits — V2 must not weaken them |

---

## 2. HARD RULES — violating any of these is a failed task

```
❌ NEVER  commit to main. Work on a branch.
❌ NEVER  place, modify, or cancel a live order.
❌ NEVER  flip or touch the LIVE_ENABLED gate (config/LIVE_ENABLED.*).
❌ NEVER  restart livr-shadow.
❌ NEVER  run `pm2 start ecosystem.config.js`  — it restarts hermes-gateway and
          kills the agent mid-turn. Restart PM2 daemons BY NAME only.
❌ NEVER  loosen .gitignore or the deploy-key scoping (three repos).
❌ NEVER  force-push. `git pull --ff-only` only.
❌ NEVER  hand-edit ledger JSON or state JSON.
❌ NEVER  blend paper and real records — they are separate books, always.
❌ NEVER  change risk limits, leverage caps, or the kill switch.
❌ NEVER  let a notification failure break a trade (Telegram send is best-effort).
```

**If a task seems to require one of these, stop and report it — do not proceed.**

---

## 3. Deploy discipline (non-negotiable)

```
1. Branch from main:            git checkout -b v2/<area>
2. Implement + tests
3. ./venv/bin/python -m pytest      ← must stay 482 passed / 3 skipped
4. Commit with a clear message
5. Report the branch and the test result
```

**Read diffs line by line, not summaries.** The repo's own rule (AGENTS.md §3): never
treat a diff as cosmetic without reading the lines.

---

## 4. Build order — do it in this sequence

Each stage is independently testable. **Do not start stage N+1 before stage N passes.**

```
STAGE 1  Contracts as code
         Pydantic (or dataclass) models for all nine contracts.
         Serialisation + validation + round-trip tests.
         No I/O, no network, no side effects. Pure.
         ← If this is wrong, everything above it is wrong.

STAGE 2  Contract D — the Jev Adapter, with the fallback
         This is the critical piece. Build the fallback FIRST, test it in
         isolation, then add the Jev call in front of it.

STAGE 3  Contract G — Risk Decision
         Deterministic. Every check returns pass/fail with a detail string.
         Unit-test each veto independently.

STAGE 4  Shadow mode (V2.0)
         Wire the chain end-to-end with NO execution:
           market_event -> feature_snapshot -> decision_state -> adapter
             -> policy -> risk -> (log only)
         Compare adapter output against the current system's decisions.
         Nothing executes in this stage.

STAGE 5  Paper mode (V2.1)
         Same chain, output to the paper book. Still no live orders.

STAGE 6  Restricted live (V2.2)  ← STOP. Owner approval required. Do not do this.
```

---

## 5. The five things most likely to go wrong

**1. Making the fallback a second hot path.**
The whole point of Contract D is that a fallback decision is *weaker* and *labelled*.
It must be capped below the `EXECUTE` tier. If your implementation lets a fallback
decision execute, the design has failed.

**2. Coercing malformed Jev output.**
If Jev returns `"confidence": "high"`, that is a **rejection**, not a value to convert.
Coercion turns an invalid response into `0.0`, which becomes a veto, which becomes a
silently missed trade. Validate against the schema and fail loudly.

**3. Letting a proposal carry its own size.**
Contract F has no `size` field *by design*. Size is computed in Contract G from equity
and risk fraction. If you find yourself adding `size` to a proposal to "make it work",
you have broken the safety property the whole architecture rests on.

**4. Adding resident processes.**
Redis/Postgres are named in the architecture note. On a 3.9 GB box with a 946 MB shadow
process, each is a real cost. **Prefer SQLite (already in use, 9.5M candles) unless there
is a demonstrated need.** Flag it if you think there genuinely is.

**5. Silent failure.**
Every component must log enough to prove it ran and did something. A stage that "passes"
because it produced no output is the failure mode this system has already suffered once.

---

## 6. Testing requirements

```
Unit        : every contract validates; every risk check has a veto test
Round-trip  : serialise -> deserialise -> identical
Fallback    : force timeout / 500 / malformed JSON / schema violation → all
              must produce source="fallback" with a reason, never a crash
Determinism : risk engine gives identical output for identical input, 1000x
Integration : full chain in shadow mode, no execution, logs a decision
```

**A test that passes because nothing was asserted is worse than no test.**

---

## 7. What NOT to build yet

```
❌ Anything that places orders
❌ Adaptive threshold tuning (needs production data first)
❌ The event bus / Redis / Postgres (evaluate need first, see §5.4)
❌ Modifications to the live path
❌ Changes to strategy logic — the strategies in the fallback are already
   OOS-validated; do not "improve" them while porting
```

---

## 8. Definitions you must not guess at

- **HTF** = Higher Timeframe (4h/1d direction), not high frequency
- **Jev** = TypeSafe's System One decision model (`POST /v1/systemone`)
- **The fallback** = `trend_follower`, `donchian_ensemble`, `tsmom`, `ctrend` —
  existing, already validated, already in the repo. **Use these names.**
- **Paper** and **real** are separate books and are never merged
- **`EXECUTE` tier** = the only tier where an order can result, and never for a fallback

---

## 9. Reporting back

When you finish a stage, report:

```
Branch    : v2/<area>
Tests     : 482 passed / 3 skipped (or the new number, with an explanation)
Files     : what you added/changed
Decisions : anything you had to interpret, and how you resolved it
Open      : anything the spec did not cover
```

**Report honestly.** A stage that is 80% done and reported as 80% is far more useful than
one reported as complete. The person reading this has been burned by false success
signals on this exact system.

---

## 10. Final reminder

The running system **places real orders with real money**. Everything in this guide
exists to let you build V2 without touching it. If you ever find yourself unsure
whether an action is safe, the answer is to stop and report — not to try it.
