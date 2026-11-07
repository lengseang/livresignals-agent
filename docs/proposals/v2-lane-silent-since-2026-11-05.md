# E13 · The V2 paper lanes have been dead for 40 hours and every watcher said nothing (2026-11-07)

**Severity: High (owner-facing).** Not a money path — `trading.v2` has no order
path (`preflight.json`: `paper_only: PASS`, "no order path in trading.v2") — but
the top8 scale hold and the entire V2 paper evidence base have been frozen since
2026-11-05T09:01Z, and the owner was never told.

## What is true right now (measured 2026-11-07T01:0xZ)

```text
paper   heartbeat 2026-11-05T09:00:03Z  cycles 1728  steps 8639
paper-b heartbeat 2026-11-05T09:00:03Z  cycles 1728  steps 8639
preflight (paper)  ok=false
  FAIL jev_key_authenticates  HTTP 403 (revoked/rotated/mistyped)
gate      both lanes: heartbeat is 2405 min old (40.1 h), rc=1
processes  pm2 restart-looping: run-v2.sh a / run-v2.sh b
last runner log line  2026-11-05 09:01:00  trading.v2.runner interrupted
```

`v2-paper.out.log` contains **427 consecutive `PREFLIGHT FAILED`** since
2026-11-05T09:01:08Z. The last good preflight was 2026-10-05T21:00:58Z.

## The five-link chain that hid it

1. **The input died without a message.** `run-v2.sh` runs
   `preflight --out … –symbols …`; on failure it sends a Telegram notice and
   sleeps 300 s. `notify_state.json` does not exist in either lane directory, so
   the `|| true` dropped a failing send. The visible signal is a log line in a
   file nobody reads, 427 times.
2. **The in-process watchdog cannot report its own process's death.**
   `src/trading/v2/watchdog.py` detects `runner_silent` correctly — keyed on
   `heartbeat.json`'s **mtime** — but the file it stats is written by
   `runner.py` in the same process tree. `watchdog_state.json` was last written
   2026-11-07T01:00:03Z and still says `{"active":["runner_silent"],
   "alerted":["runner_silent"]}` — a *remembered* state, not a running one.
3. **The independent watcher sees it and exits 1.** `scripts/v2_gate_watchdog.py`
   (read-only check, correct) prints
   `paper: heartbeat is 2405 min old — the lane is not cycling` and returns 1.
4. **Its wrapper discards the exit code.** `~/hermes-home/scripts/v2_gate_roll.sh`
   handles rc=2 (transient) and rc=1 (roll back to top3) but **both branches end
   in `exit 0`**. The scheduler therefore records `completed` — `eb407360fa04`
   (v2-scale-gate-check) has **4 completed, 0 failed**. The one job that keeps
   its rc, `d071de6fa4a4` (v2-top8-gate-watchdog), has **4 failed** and the
   `V2 GATE FAILURE` text did reach the owner — twice a day.
5. **The digest quotes the frozen numbers as health.** `watchdog.digest()` prints
   `jev 1575/1725`. That is `jev_ok/jev_calls` from the heartbeat written
   2026-11-05T09:00:03Z. **A ratio of two frozen counters is a frozen ratio**:
   it reads 91.3% turnout for a lane that has not taken a step in 40 hours.

So the owner did receive gate-failure text — 4 times — and the alarm was
correct. What is missing is the *cause*: the heartbeat sentence is identical
for "the runner is slow/looping" and "the runner cannot start at all", and the
rollback the gate recommends (set `V2_SYMBOLS=top3`, restart) cannot fix a 403.

## Why the lane cannot recover by itself

The failure is a credential: `TYPESAFE_API_KEY` (or its rotation) in
`config/v2.env`. Every restart since 2026-11-05 09:00 fails preflight in 300 s
and retries. **Restarting is not a fix and I did not restart anything.**

## What I shipped (safe, repo-only, no lane/process/config touched)

| File | What |
|---|---|
| `research/_v2_lane_silence_probe.py` | read-only reproduction: frozen heartbeat, preflight verdict, watchdog locality, wrapper rc handling, scheduler delivery rows |
| `tests/loop/test_v2_lane_silence.py` | 6 tests pinning the chain; the wrapper test fails the day rc is preserved, on purpose |
| `docs/proposals/v2-lane-silent-since-2026-11-05.md` | this note |

Mutations run: `heartbeat_is_stale` → `False` ⇒ 1 failed; `jev_turnout` → `0.0`
⇒ 1 failed. Harness restored by `diff -q`, identical. `pytest
tests/loop/test_v2_lane_silence.py -q` ⇒ **6 passed**.

## Options — the owner picks; I recommend (b) + (c)

**(a) Leave it.** Cost: two lanes frozen; the top8 hold is not progressing, and
the 18,742 already-logged decisions stop accruing. Cheap, honest, slow.

**(b) Fix the credential, then unpause (recommended first step).** Supply a
working `TYPESAFE_API_KEY` (or rotate it) in `/opt/trading-agent-v2/config/v2.env`
and `pm2 restart v2-paper v2-paper-b`. Preflight will pass and the heartbeat
mtime moves. **I cannot do this** — no key, and the file is the owner's.

**(c) Make the rc reach the owner (recommended, independent of b).** Three-line
change in `v2_gate_roll.sh`, same template as E11:

```bash
OUT="$(python3 …/v2_gate_check.py 2>&1)"; RC=$?
if [ "$RC" = "2" ]; then echo "$OUT"; echo "(transient)"; exit "$RC"; fi
if [ "$RC" != "0" ]; then echo "$OUT"; echo "--- rolling back ---"; …; exit "$RC"; fi
exit 0
```

That turns a `completed`+empty row into a delivered `V2 GATE FAILURE` line that
also says *why*: add one line to `v2_gate_watchdog.py`'s stale branch that reads
`preflight.json` and prints its failing required checks. The gate already knows
how to be silent when healthy; it just needs a cause line when not.

**(d) Move the watchdog out of the runner's process.** Optional, larger: the
`runner_silent` rule is correct but cannot fire on its own death. A separate
cron entry (the gate script already is one) is the fix; the in-process one then
becomes a second opinion rather than the only one.

## Not done, deliberately

- No restart, no `config/v2.env` edit, no pm2 command, no `V2_SYMBOLS` change.
- No trading parameter touched; nothing in `trading-agent`'s real-money paths.
- The seam "the lane is dead" is stated as an outage, not evidence that the
  signal is bad — the 18,742 decisions were all `NO_TRADE` (see E12 family).

## Pre-mortem: it is 3 months later and this failed again — why?

1. **The key is rotated again and the same `|| true` swallows the notice.** The
   preflight notice has no state file, so it cannot rate-limit itself and its
   failure is invisible; that is why (c) matters more than (b).
2. **Someone reads `digest()` and believes `jev 1575/1725`.** The fix is a
   `heartbeat_age` field in the digest, printed only when the age exceeds the
   runner's own 5-minute budget. Without it the digest is a photo of a
   corpse labelled "resting".
3. **A future re-template restores `exit 0`.** That is exactly what
   `tests/loop/test_v2_lane_silence.py::test_the_wrapper_does_not_discard_the_gate_exit_code`
   now fails on.

**Needed:** (b) a working `TYPESAFE_API_KEY` in `config/v2.env` and a restart, and
(c) yes/no to preserving `v2_gate_roll.sh`'s rc + adding a cause line to the
stale branch. Nothing else on this list needs a decision to be useful.
