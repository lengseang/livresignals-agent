# Prompt for the VPS agent: upgrade V2 to rc4 and scale the symbol set (3 → 8 → 15)

Paste everything below the line into the agent on the VPS. Use it ONLY after the rc3 deployment has run clean for at least
2 days (the owner confirms this).

---

**Release commit to deploy: `RELEASE_SHA_GOES_HERE`** (the head of the branch `v2/release-rc4`).

You are upgrading the already-running **V2 paper service** (isolated worktree `/opt/trading-agent-v2`) to release rc4, then
widening its symbol set in stages. Report evidence (command output), not claims. If any step fails or does not match what is
described, STOP and report; do not improvise.

## Hard rules: never break these

* NEVER commit, merge or push; NEVER touch `main`; NEVER force-push. Only `git fetch` in `/opt/trading-agent`.
* NEVER touch the live checkout `/opt/trading-agent`'s files, processes, `config/LIVE_ENABLED*`, `.gitignore`, risk limits, or
  any ledger/state JSON. NEVER run `pm2 start ecosystem.config.js`; never stop or restart any pm2 app except
  `v2-paper`, `v2-paper-b`, `v2-watchdog`, `v2-daily`.
* NEVER print or `cat` `config/v2.env` or the key. NEVER place, modify or cancel an exchange order.
* NEVER delete anything under `/opt/trading-agent-v2/data/v2/`: the chains are append-only evidence.
* If `node -e ""` aborts on this box (a known host issue), do NOT touch system packages; run pm2 through the working node
  exactly as you did at the first deployment.

## Step 1: baseline (report all output)

```
cd /opt/trading-agent-v2 && git rev-parse HEAD
pm2 status
for L in paper paper-b; do python3 -c "import json;h=json.load(open('data/v2/$L/heartbeat.json'));print('$L',{k:h.get(k) for k in ('steps','cycles','jev_ok','jev_calls','kill_engaged','positions','outcomes')})"; done
```

Both lanes must be online, `kill_engaged` false and `positions` understood. If a position is open, that is fine: the book
is rebuilt from its chain on restart.

## Step 2: move to rc4 (the same procedure as any upgrade)

```
cd /opt/trading-agent && git fetch origin v2/release-rc4 && git rev-parse origin/v2/release-rc4    # must equal the SHA above
cd /opt/trading-agent-v2
touch data/v2/paper/STOP data/v2/paper-b/STOP && sleep 90 && pm2 stop v2-paper v2-paper-b
git checkout --detach <that SHA>
set -o pipefail
PYTHONPATH=src venv/bin/python -m pytest -q tests/loop 2>&1 | tail -3 ; echo "pytest exit code: ${PIPESTATUS[0]}"   # must be 0
rm -f data/v2/paper/STOP data/v2/paper-b/STOP
```

If the tests do not pass, STOP and report; do not restart on a failing release (roll back with
`git checkout --detach <previous SHA from Step 1>` and restart).

Do not change `V2_SYMBOLS` yet. Restart on the SAME 3 coins first, to prove rc4 itself:

```
pm2 restart v2-paper v2-paper-b      # (or via the working node, as before)
```

Wait about 20 minutes (one 15-minute boundary plus margin) and verify with `heartbeat.json` of both lanes:
`steps` rising, `cycles` > 0, `timing.step_ms_last` present, `by_symbol` has the 3 coins with `unusable` 0 or 1 each (the
boundary right after a restart can be unusable), `jev_budget` present with `refused` 0, `restored` shows the restart replay.
Also confirm in the startup output (`pm2 logs v2-paper --lines 40 --nostream`) that preflight printed
`PASS  jev_key_authenticates`, and that `jev_ok` in the heartbeat is greater than 0 once a decision boundary has passed. If
preflight shows `FAIL jev_key_authenticates`, the key in `config/v2.env` is wrong or revoked: STOP and tell the owner (do not
read the key back); the service will not start until it is fixed, by design.
Then run the watchdog for each lane (`PYTHONPATH=src venv/bin/python -m trading.v2.watchdog --out data/v2/<lane>`): `problems`
must be `{}`.

## Step 3: widen to top8 (ONLY if Step 2 is clean)

Edit `V2_SYMBOLS=top8` in `/opt/trading-agent-v2/config/v2.env` (edit the one line; do not display the file), then
`pm2 restart v2-paper v2-paper-b`. Hold for **2 days**, checking twice a day. The gates (BOTH lanes):

| gate | read | must be |
|---|---|---|
| stale decisions | `by_symbol[*].unusable / cycles` | under 1% per coin once there are 8+ boundaries |
| slowest step | `timing.step_ms_max` | under 15000 |
| oldest snapshot | `timing.stale_ms_max` | under `timing.stale_ceiling_ms` |
| book prefetch | `book_prefetch` | `hit` much larger than `stale_refetch` |
| Jev budget | `jev_budget` | `used` under 70% of `cap`, `refused` 0 |
| Jev answering | `jev_ok / jev_calls` | at least 90% |
| errors | `outcomes.cycle_error`, watchdog `problems` | 0 and `{}` |

If ANY gate fails: set `V2_SYMBOLS=top3`, restart both lanes, and report the numbers. Do not try other fixes.

## Step 4: widen to all15 (ONLY after 2 clean days on top8 and the owner's go-ahead)

Same as Step 3 with `V2_SYMBOLS=all15`. Hold 2 days with the same gates.

## Report format (after each step)

1. The release SHA now checked out and the pytest line with its exit code.
2. `pm2 status` (only the four V2 apps matter; confirm every other app is unchanged from Step 1).
3. For each lane: `timing`, `by_symbol` (compact), `book_prefetch`, `jev_budget`, `jev_ok/jev_calls`, `kill_engaged`.
4. Watchdog output for each lane.
5. Anything unexpected, verbatim.
