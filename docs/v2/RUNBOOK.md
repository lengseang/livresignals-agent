# V2 paper service: runbook

**Scope.** This runs the V2 decision system as a resident PAPER service.

* It uses public Binance market data, real Jev decisions and a simulated book.
* `trading.v2` contains no order path, and preflight proves it on every start.
* Real money is a separate decision. See "Live-readiness gate" at the end.

**Where it runs.** On the VPS, in an isolated git worktree at **`/opt/trading-agent-v2`**, pinned to a release
tag.

* The live box at `/opt/trading-agent` keeps its checkout and its processes. V2 does not touch them.
* V2 does not touch `config/LIVE_ENABLED*`, the `livr-shadow` service or the repo's `ecosystem.config.js` either.

## Layout

| path | what |
|---|---|
| `/opt/trading-agent-v2` | git worktree at tag `v2.x.y` (detached) |
| `/opt/trading-agent-v2/venv` | symlink to `/opt/trading-agent/venv` (same Python 3.12 + numpy/pydantic) |
| `/opt/trading-agent-v2/config/v2.env` | `TYPESAFE_API_KEY`, `V2_OUT`, `V2_SYMBOLS`, `V2_CADENCE`; chmod 600; git-ignored |
| `$V2_OUT` (default `/opt/trading-agent-v2/data/v2/paper`) | hash-chained records, `heartbeat.json`, `preflight.json`, `scorecard.json`, watchdog state |
| `/opt/trading-agent-v2/logs/` | pm2 stdout/stderr |

**pm2 apps**, from `deploy/v2/ecosystem.v2.config.js` only:

* `v2-paper`: the runner, started through `run-v2.sh`, with preflight first.
* `v2-watchdog`: runs every 5 minutes and alerts on problems.
* `v2-daily`: runs at 08:50 UTC; scorecard plus a Telegram digest.

## Start, stop, restart

```
cd /opt/trading-agent-v2
pm2 start deploy/v2/ecosystem.v2.config.js        # NEVER `pm2 start ecosystem.config.js`
pm2 status v2-paper ; pm2 logs v2-paper --lines 50
touch "$V2_OUT/STOP"                               # graceful stop at the next step (then: pm2 stop v2-paper)
pm2 restart v2-paper                               # restart: the paper book is rebuilt from its own chain
```

* **Kill switch.** Run `touch "$V2_OUT/V2_KILL"`. It latches and blocks every new order, and the watchdog alerts.
  Only a human can reset it: remove the file and restart.
* **Never edit the chains** (`$V2_OUT/v2cycle`, `v2paper`, `v2kill`). They are append-only and hash-linked;
  preflight refuses to start on a broken one.

## Verify it is healthy

```
cd /opt/trading-agent-v2 && PYTHONPATH=src venv/bin/python -m trading.v2.preflight --out "$V2_OUT"
cat "$V2_OUT/heartbeat.json"           # updated every minute
PYTHONPATH=src venv/bin/python -m trading.v2.scorecard --out "$V2_OUT"
PYTHONPATH=src venv/bin/python -m trading.v2.watchdog --out "$V2_OUT"
```

Heartbeat fields that matter:

| field | what it tells you |
|---|---|
| `cycles`, `outcomes` | the run is doing work |
| `jev_ok` / `jev_calls` | how much Jev is answering |
| `fallback_gateway` | the gateway took over |
| `dual_run`, `dual_run_advice` | Jev-vs-gateway agreement |
| `context` | multi-day context ok / missing; `refresh_ms` |
| `exits` | stopped / target / ratchet exits |
| `stops_tightened` | stop amendments, by source |
| `kill_engaged`, `kill_reasons` | the kill switch |
| `restored` | set after a restart |

## Upgrade (new release tag)

```
cd /opt/trading-agent && git fetch origin --tags
cd /opt/trading-agent-v2 && touch "$V2_OUT/STOP" && sleep 90 && pm2 stop v2-paper
git checkout --detach v2.x.y                      # the worktree moves to the new tag; data/ and config/ are untouched
PYTHONPATH=src venv/bin/python -m pytest -q tests/loop
rm -f "$V2_OUT/STOP" && pm2 restart v2-paper && pm2 logs v2-paper --lines 30
```

## Roll back

```
cd /opt/trading-agent-v2 && touch "$V2_OUT/STOP" && sleep 90 && pm2 stop v2-paper
git checkout --detach <previous tag> && rm -f "$V2_OUT/STOP" && pm2 restart v2-paper
```

The chains are append-only and stay valid across versions. A restored book replays them.

## Rotate the Jev key

Edit `config/v2.env`, run `chmod 600 config/v2.env` and `pm2 restart v2-paper`. The key is read from the
environment at each call and is never logged. The key used during development MUST be rotated before this
service starts.

## Costs and growth (measured)

* **Jev:** ~2.6k input tokens per entry call and ~1.6k per position call. With 3 symbols every 15 minutes, that is
  288 calls a day.
* **Disk:** the cycle records hold the full snapshot, state, decision and dual-run, about 2-3 MB a day for 3
  symbols. Watch it in the digest; preflight and the watchdog alert below 2 GB free.

## What the alerts mean (Telegram via `hermes send`)

| alert | action |
|---|---|
| `runner_silent` | `pm2 logs v2-paper`; preflight; restart |
| `kill_engaged` | read `kill_reasons`; a human decides; remove `V2_KILL` and restart only when understood |
| `jev_failing` | the gateway is answering (entries cannot execute). Check the key, balance and `api.typesafe.ai` |
| `feed_failing` | Binance data API trouble; the runner retries every minute |
| `chain_broken` | STOP. Do not delete anything. Investigate: the record is evidence |
| `disk_low` | free space; never delete chain files |
| `cycle_errors` | `pm2 logs`; the record names the stage |

## Live-readiness gate (real money). NOT met; none of this is built

1. At least 30 days of this paper service with 0 cycle errors and chains that verify.
2. A positive, statistically supported expectancy in paper, or an admitted entry rule (see
   `docs/v2/V2-15-RESULTS.md` and `docs/v2/V2-16-RESULTS.md`: none is admitted today).
3. Real fees and slippage measured from real fills (testnet or tiny live).
4. An execution adapter with order reconciliation and an exchange-side bracket (OCO), with its own tests.
5. The owner's written decision, plus `LIVE_ENABLED`. V2 never touches that file.
