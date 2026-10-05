# V2 paper service: runbook

**Scope.** This runs the V2 decision system as a resident PAPER service.

* It uses public Binance market data, real Jev decisions and a simulated book.
* `trading.v2` contains no order path, and preflight proves it on every start.
* Real money is a separate decision. See "Live-readiness gate" at the end.

**Where it runs.** On the VPS, in an isolated git worktree at **`/opt/trading-agent-v2`**, pinned to the exact commit of a release branch (`v2/release-*`).

* The live box at `/opt/trading-agent` keeps its checkout and its processes. V2 does not touch them.
* V2 does not touch `config/LIVE_ENABLED*`, the `livr-shadow` service or the repo's `ecosystem.config.js` either.

## Layout

| path | what |
|---|---|
| `/opt/trading-agent-v2` | git worktree, detached at the commit of release branch `v2/release-*` |
| `/opt/trading-agent-v2/venv` | symlink to `/opt/trading-agent/venv` (same Python 3.12 + numpy/pydantic) |
| `/opt/trading-agent-v2/config/v2.env` | `TYPESAFE_API_KEY`, `V2_OUT`, `V2_SYMBOLS`, `V2_CADENCE`; chmod 600; git-ignored |
| `$V2_OUT` / `$V2_OUT_B` (default `.../data/v2/paper` and `.../paper-b`) | hash-chained records, `heartbeat.json`, `preflight.json`, `scorecard.json`, watchdog state |
| `/opt/trading-agent-v2/logs/` | pm2 stdout/stderr |

**pm2 apps**, from `deploy/v2/ecosystem.v2.config.js` only:

* `v2-paper`: lane A, the approved fixed 3.5% / 7% bracket (`run-v2.sh a`), with preflight first.
* `v2-paper-b`: lane B, the volatility-scaled bracket (owner decision E16, `run-v2.sh b`), with its own book and chains in `$V2_OUT_B`.
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

* **Kill switch.** Run `touch "$V2_OUT/V2_KILL"` (per lane). It latches and blocks every order that ADDS risk;
  reduce-only exits still pass (owner decision E14). The watchdog alerts.
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

## Upgrade (new release branch)

```
cd /opt/trading-agent && git fetch origin v2/release-<next> && git rev-parse origin/v2/release-<next>   # = the SHA the owner gives
cd /opt/trading-agent-v2 && touch "$V2_OUT/STOP" && sleep 90 && pm2 stop v2-paper
git checkout --detach <that SHA>                  # data/ and config/ are untouched
PYTHONPATH=src venv/bin/python -m pytest -q tests/loop
rm -f "$V2_OUT/STOP" && pm2 restart v2-paper && pm2 logs v2-paper --lines 30
```

## Roll back

```
cd /opt/trading-agent-v2 && touch "$V2_OUT/STOP" && sleep 90 && pm2 stop v2-paper
git checkout --detach <previous release SHA> && rm -f "$V2_OUT/STOP" && pm2 restart v2-paper
```

The chains are append-only and stay valid across versions. A restored book replays them.

## Rotate the Jev key

Edit `config/v2.env`, run `chmod 600 config/v2.env` and `pm2 restart v2-paper`. The key is read from the
environment at each call and is never logged. The key used during development MUST be rotated before this
service starts.

## Costs and growth (measured)

* **Jev:** ~2.6k input tokens per entry call and ~1.6k per position call. With 3 symbols every 15 minutes, that is
  288 calls a day per lane (576 with lane B).
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


## Scaling the symbol set (rc4)

`V2_SYMBOLS` (and `--symbols`) take pairs and/or presets: **`top3`** (BTC, ETH, SOL), **`top8`** (+ XRP, BNB, ADA, LINK,
DOGE) and **`all15`** (+ AVAX, NEAR, SUI, ARB, DOT, LTC, ATOM). The multi-day context is always computed over all 15
whatever you trade, because the cross-coin ranks need them.

**Rollout gates.** Move up only when the current step has run at least 2 days and every gate holds on BOTH lanes. Drop back
one step if any gate fails; the chains are append-only, so nothing is lost.

| gate | where to read it | must be |
|---|---|---|
| decisions skipped as stale | `heartbeat.json` `by_symbol[*].unusable` / `cycles` | under 1% per coin (the first boundary after a restart is expected) |
| slowest step | `timing.step_ms_max` | under 15 000 ms |
| oldest snapshot used | `timing.stale_ms_max` | under `timing.stale_ceiling_ms` (20 000) |
| order-book prefetch | `book_prefetch` | `hit` much larger than `stale_refetch` |
| Jev budget | `jev_budget.used` / `cap` | under 70% of the daily cap, `refused` 0 |
| Jev answering | `jev_ok` / `jev_calls` | at least 90% (if it is near 0, check `jev_key_authenticates` in `preflight.json`: a rotated key means everything is answered by the gateway) |
| errors | `outcomes.cycle_error`, watchdog `problems` | 0 and `{}` |

**Changing the set.** Edit `V2_SYMBOLS` in `config/v2.env`, then `pm2 restart v2-paper v2-paper-b`. The paper book is rebuilt from
its chain, so open positions survive; the next boundary decides for the new set. Do not shrink the set while positions are
open in a coin you are removing.

**What rc4 changed for scale.** The network phases (priming, hourly context refresh, daily stances, each symbol's bars and
order book) run concurrently; decisions, Jev, the paper book and the chains stay sequential in the configured order, so
runs are as reproducible as before. Before, about 2 s per symbol meant 15 symbols would have aged the last third of the
decisions past the 20 s staleness ceiling. The ceiling itself is unchanged.

**Jev call budget.** `jev_daily_call_cap` (4 000 per lane per UTC day in the approved config) is shared by the entry and
position clients. Over the cap the gateway answers, labelled `balance_exhausted`, and the watchdog alerts at 90%.
