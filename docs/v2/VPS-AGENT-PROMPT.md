# Prompt for the VPS agent: deploy the V2 PAPER service (release branch v2/release-rc3)

Paste everything below the line into the agent on the VPS.

---

**Release commit to deploy: `RELEASE_SHA_GOES_HERE`** (the owner fills this in. It is the head of the branch
`v2/release-rc3`. Where this prompt says "the SHA the owner gives you", it means this value.)

You are deploying the **V2 paper trading service** of `lengseang/livresignals-agent` on this VPS. Follow these steps
exactly. Report evidence (command output), not claims. If any step fails or does not match what is described, STOP
and report: do not improvise a workaround.

## What this is

* V2 makes trading decisions with Jev (TypeSafe System One). It falls back to a deterministic gateway when Jev is
  down. It manages open positions (take-profit, a lock-half stop ratchet, Jev position advice), and it fills
  everything in a SIMULATED paper book from public Binance data.
* `trading.v2` contains **no order path**: no exchange client and no order call. Its preflight checks that on every
  start.
* It runs from an **isolated git worktree** at `/opt/trading-agent-v2`, pinned to the exact commit of the release branch `v2/release-rc3`. The live
  checkout `/opt/trading-agent` and its running processes stay exactly as they are.

## Hard rules: never break these

* NEVER commit, merge or push anything. NEVER touch `main`. NEVER force-push. NEVER `git reset --hard` or
  `git clean` in `/opt/trading-agent`.
* NEVER change the checkout or branch of `/opt/trading-agent`. Only `git fetch` there (fetching does not touch the
  working tree).
* NEVER run `pm2 start ecosystem.config.js`, and NEVER restart, stop or modify any existing pm2 app or the
  `livr-shadow` service. Only the four new apps `v2-paper`, `v2-paper-b`, `v2-watchdog` and `v2-daily` are yours.
* NEVER touch `config/LIVE_ENABLED*`, `.gitignore`, deploy-key settings, risk limits or any ledger or state JSON.
* NEVER print, log or echo the API key. Do not `cat` `config/v2.env`.
* NEVER place, modify or cancel an exchange order. This deployment has no reason to.
* NEVER request or copy the whole `data/` directory. It contains credentials.
* Do not install system packages. Do not create Redis, Postgres or other resident services.

## Step 0: look before touching (report all output)

```
cd /opt/trading-agent && git status -sb | head -5 && git log --oneline -1
pm2 status
df -h /opt | tail -1 ; free -m | head -2
/opt/trading-agent/venv/bin/python -V
ls -d /opt/trading-agent-v2 2>/dev/null && echo "EXISTS" || echo "absent"
```

* If `/opt/trading-agent-v2` already exists, STOP and report. Do not overwrite it.
* If Python is not 3.12.x, STOP and report.

## Step 1: fetch the release (fetch only; the live working tree is not changed)

```
cd /opt/trading-agent
git fetch origin main
git fetch origin v2/release-rc3
REL=$(git rev-parse origin/v2/release-rc3); echo "release commit: $REL"
git merge-base --is-ancestor origin/main "$REL" && echo "release contains main"
git diff --stat origin/main "$REL" -- . ':(exclude)src/trading/v2' ':(exclude)tests/loop' ':(exclude)docs' ':(exclude)deploy/v2' | tail -3
```

* Expected: the release commit EQUALS the SHA the owner gives you (if not, STOP: the branch moved), "release
  contains main" is printed, and the last diff is EMPTY. V2 only adds files
  under `src/trading/v2`, `tests/loop`, `docs` and `deploy/v2`.
* If the diff is not empty, STOP and report.

## Step 2: create the isolated worktree

```
git -C /opt/trading-agent worktree add --detach /opt/trading-agent-v2 "$REL"
cd /opt/trading-agent-v2
ln -s /opt/trading-agent/venv venv
mkdir -p config logs data/v2/paper data/v2/paper-b
cp deploy/v2/v2.env.example config/v2.env && chmod 600 config/v2.env
git -C /opt/trading-agent status -sb | head -3     # must be unchanged from Step 0
```

## Step 3: dependencies and tests (no network, no orders)

```
cd /opt/trading-agent-v2
venv/bin/python -c "import numpy, pydantic; print(numpy.__version__, pydantic.__version__)"
set -o pipefail
PYTHONPATH=src venv/bin/python -m pytest -q tests/loop 2>&1 | tail -3 ; echo "pytest exit code: ${PIPESTATUS[0]}"   # must be 0
```

* Expected: about `1266 passed, 3 skipped`, 0 failed, and `pytest exit code: 0`. (The tests do not depend on how the
  host names its venv, and they skip the pm2-file check where `node` itself aborts.)
* If numpy or pydantic is missing, STOP and report. Do not pip-install into the shared venv without the owner's
  approval.
* If any test fails, STOP and report the failure output.

## Step 4: the key (the OWNER does this)

Ask the owner to put a **new** TypeSafe key into `/opt/trading-agent-v2/config/v2.env` (the line
`TYPESAFE_API_KEY=`). The key used during development must be rotated, not reused.

* Also confirm `HERMES_BIN` and `HERMES_HOME` in that file point to the box's hermes. Run `which hermes` and
  `ls /home/ubuntu/hermes-venv/bin/hermes` to check.
* Do not read the key back. Check that it is set with:

```
grep -c '^TYPESAFE_API_KEY=.\+' /opt/trading-agent-v2/config/v2.env    # must print 1
```

## Step 5: preflight (must print PREFLIGHT OK)

```
cd /opt/trading-agent-v2 && set -a && . config/v2.env && set +a
PYTHONPATH=src venv/bin/python -m trading.v2.preflight --out "$V2_OUT" --symbols "$V2_SYMBOLS"
```

Report the full table. Every line must be PASS. If anything FAILS, STOP and report it. Do not start the service.

## Step 6: start ONLY the V2 apps

```
cd /opt/trading-agent-v2
pm2 start deploy/v2/ecosystem.v2.config.js
pm2 status
```

**Known host issue.** If `node -e ""` aborts on this box (a broken system node), do NOT touch system packages. Run pm2's
own script under a working node instead, changing nothing: `<working-node> $(readlink -f $(which pm2)) start
deploy/v2/ecosystem.v2.config.js`. Do not modify pm2 or its daemon. Do not run `pm2 save` until the owner has decided
about the node problem; report it instead.

Expected: `v2-paper` (lane A, approved fixed bracket) and `v2-paper-b` (lane B, volatility-scaled bracket) are online;
`v2-watchdog` and `v2-daily` are registered as cron apps. Every pre-existing app is
exactly as it was in Step 0.

## Step 7: verify it is really working (wait about 20 minutes, then report)

```
pm2 logs v2-paper --lines 60 --nostream ; pm2 logs v2-paper-b --lines 60 --nostream
cat /opt/trading-agent-v2/data/v2/paper/heartbeat.json
cat /opt/trading-agent-v2/data/v2/paper-b/heartbeat.json
cd /opt/trading-agent-v2 && for L in paper paper-b; do PYTHONPATH=src venv/bin/python -m trading.v2.watchdog --out data/v2/$L; done
```

Report, for EACH lane:

* `steps` is increasing (it is written every minute).
* `cycles` > 0 after the first 15-minute boundary.
* `jev_ok` versus `jev_calls`. Most answers should come from Jev; if they don't, report `fallback_gateway`.
* `context.ok` > 0 and `context.missing`.
* `kill_engaged` is false.
* `outcomes` and `positions`.
* The watchdog output: `problems` should be `{}`.
* Whether the Telegram "[V2 paper] starting" message arrived.

## Step 8: register the worktree with pm2's resurrect list

Only do this if the owner confirms after Step 7:

```
pm2 save
```

## Stop / rollback (only if the owner asks or something is wrong)

```
touch /opt/trading-agent-v2/data/v2/paper/STOP /opt/trading-agent-v2/data/v2/paper-b/STOP ; sleep 90 ; pm2 stop v2-paper v2-paper-b v2-watchdog v2-daily
```

* Do not delete anything under `data/v2/`. The chains are append-only evidence.
* Full instructions are in `/opt/trading-agent-v2/docs/v2/RUNBOOK.md`.

## Final report format

1. Step 0 baseline (git status, pm2 status before).
2. Release verification (tag hash, ancestor check, empty diff).
3. Test count.
4. The preflight table.
5. pm2 status after.
6. Heartbeat and watchdog after about 20 minutes.
7. Anything unexpected, verbatim.
