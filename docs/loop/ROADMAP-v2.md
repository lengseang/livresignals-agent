# Roadmap v2: the lab runs itself, the AI audits, the owner deploys

Written 2026-09-26 after waves 1–3. It supersedes the phase order in ROADMAP.md from here on;
that file stays as history.

## Where we are

- **Built (PRs #2–#33, unmerged):** live tick recorder, full history (1m since 2017, 1s for 12
  months), replay engine, live scanner (proven to match replay), measured fill model, Deflated
  Sharpe gate, feed API, news ingest, and fix designs for the live book's defects F1–F4.
- **Found:** no strategy passes the gate. The trend rule makes money and beats BTC hold, but the
  evidence is too thin. Hourly strategies lose to fees. The live real-money book runs on that
  un-validated trend rule and has known defects (duplicate buy F1, stranded position with no stop F2).
- **The cost problem:** research by AI agents costs tokens per experiment. Most of that work is
  deterministic (replay, then gate, then compare) and needs no AI.

## The operating model

```
 owner / AI writes an experiment spec (pre-registered)      ── AI tokens: small, rare
                     │
                     ▼
 research/queue/*.json  ──►  LAB RUNNER (plain Python, runs all day/night)   ── 0 tokens
                               • expands grids, runs replays on N CPU workers
                               • grades every run with the gate, logs every trial
                               • enforces the holdout lock and the no-real-money rules
                               • writes data/local/lab/digest-<date>.md (one page)
                     │
                     ▼
 AI reads the digest (one page), audits anomalies,          ── AI tokens: small, daily
 writes the next specs or a PR, escalates decisions
                     │
                     ▼
 owner reviews, merges, and deploys                          ── owner time
```

## Phases

### L0 · Close the loop on what exists (owner-heavy, days)
- Owner answers **E4** (approve the F1–F4 fix designs) and **E7** (pause 3x futures), and supplies
  the **E1/E2** files (fee audit, `src/trading/data/`).
- Owner merges PRs #2–#33 in stack order into `loop/integration`.
- **Exit:** `loop/integration` holds everything; decisions recorded in ESCALATIONS.md.

### L1 · Lab runner (AI builds once, then it runs without AI) · ~1–2 sessions
- `trading.lab`: job spec schema, queue folder, deterministic grid expansion ("strategy issuer"),
  a worker pool (default 6 of 8 cores, leaving room for the recorder), resumable (a crash re-runs only
  unfinished jobs), and one lock so two runners never collide.
- Built-in guards: pre-registration is required (a spec is hashed and frozen when queued); every
  run is logged as a trial; the **holdout can be opened once per candidate, only by a spec marked
  `final_test`, and each use is written to a ledger**; no imports of exchange or real-money code;
  a daily trial budget.
- Output: `results.jsonl`, plus a one-page `digest-<date>.md`: new passes and fails, the closest
  candidates, crashes, data anomalies, disk use.
- Runs as a long-lived process, or on a Windows Task Scheduler entry the owner creates.
- **Exit:** a queued spec runs overnight with zero AI calls and produces a digest.

### L2 · Honest evidence for the best candidate (runner work, AI audits) · ~1 week of wall time
1. Persist the daily equity curve; switch the gate's DSR to **daily** returns (pre-registered
   methodology change, result accepted either way).
2. Re-grade existing runs under the new method (no new trials).
3. Freeze **trend SMA200 (15 coins, 4h, −15% stop)** as-is, then test it once on **the 38 unseen
   coins** plus **the 6-month holdout** (`final_test`).
4. With the real fee rate (E1), re-price every family; reopen the hourly families only if costs
   fall enough to matter.
- **Exit:** the first strategy with PASS on in-sample, out-of-sample, out-of-universe and holdout,
  or a documented "this family has no edge".

### L3 · Paper live on the VPS (owner deploys) · starts after L0, runs ≥ 30 days
- Deploy the paper-only pieces to the VPS: recorder, scanner, feed API, news. They have no order path.
- The scanner runs the L2 candidate live in paper; a daily job compares live paper against replay of
  the same days (they must match within tolerance, as proven locally).
- **Exit:** 30+ days of live paper whose results sit inside the replay's expected range.

### L4 · Real money (owner decision only)
- Prerequisite: **F1–F4 fixes deployed first** (T8.1). They protect whatever real money runs, even today.
- A candidate goes live only after L2 PASS + L3 match, at the smallest size, behind a flag, with a
  kill switch and daily reconciliation against the wallet.

### L5 · Operator terminal (front end)
- After L3 is producing a live feed. Needs E8 answers. Built against the T5.1 feed contract.

### M1 · Next milestone: "the terminal shows the thinking" (agreed 2026-09-26)
Order, one PR each:
1. **Lab runner (L1)**: fast data cache, timeframe-native replay, vectorised screening, parallel workers, daily digest.
2. **Explainable scans**: every scanner evaluation emits `scan_cycle` with per-rule values, thresholds, and fired or
   blocked plus the reason. This feeds the strategy board, the signal inspector and Live Scan.
3. **Paper account events**: equity, PnL, exposure (feeds the equity sparkline, the risk and exposure bars).
4. **Terminal v2** (front end, from the owner's concept): live strategy board; a signal inspector with check chips,
   score breakdown and sizing chain; a "signal field" chart mode (score vs confidence); richer side panels; strategy
   markers on candles toggleable per strategy; LIVE_PAPER vs REPLAY-run marker source (never mixed on one screen).
5. **Shared risk engine**: circuit breakers, daily-loss halt, drawdown scaler, correlation penalty, used by paper and
   future live alike (the sizing chain in the concept).
Later: replay scrubbing in the terminal, a real-book reconciliation view, Telegram alerts (dry-run first).
A composite multi-strategy score is itself a strategy: it must pass the gate before it sizes anything.

## When can we redeploy to the VPS?

There are three different deploys with three different gates:

| Deploy | What it is | Can happen | Blocked by |
|---|---|---|---|
| **A · Safety fixes to the live book** | F1–F4 (duplicate buy, stranded position without a stop, unrecorded exits, live_executor) | **First, and the most urgent**: days after approval | E4 approval → T8.1 implementation (1–2 sessions) → owner deploys |
| **B · Paper services** | recorder, scanner, feed, news on the VPS | about 1 week | owner merges to `main` and deploys; the VPS reaches Binance (E6d) |
| **C · A new strategy with real money** | the L2 candidate trading real funds | **4–6 weeks at the earliest** | L2 PASS **and** 30 days of matching live paper (L3) **and** owner decision |

Until deploy C: **pause 3x futures (E7)**, and consider cutting the trend book's size, since its rule
has not passed validation.

## Token budget rules (from the owner, 2026-09-26)
- No parallel agent waves unless asked. Compute runs in the lab runner, not in agent turns.
- The AI wakes on a digest or an owner message, not on a timer.
- One AI audit per digest: read one page, act, stop.
