# P2 · Design: the shadow scanner (forward validation, no orders)

Status: **design only, waiting on the owner** (it changes the live scanner). ROADMAP v3 stage "SHADOW".
Nothing here can place an order: no exchange client is imported (existing guard), market data comes from
the read-only mirror (`trading.stream.ws_client`, data-stream.binance.vision).

## Goal
Every candidate that is not retired runs 24/7 on the live market as a paper book, and every signal it emits is
**committed to a hash chain before its outcome exists**. After enough forward days, a strategy's forward
record (not its backtest) decides promotion. The same records feed the terminal league.

## What exists (T3.2)
`trading.scanner.Scanner`: ticks → bars → the replay engine's `LoopCore.step` (live == replay), a PaperAccount,
append-only daily signal files (`DailySink`, T6.1 envelopes). One scanner, one account.

## Design

### 1. One isolated book per candidate
`LoopCore` sends every strategy's signals to every account, so candidates must not share a core.
`ShadowFleet` owns one tick stream (one websocket) and fans each tick out to **one Scanner per candidate**,
each with its own LoopCore, BarStore and PaperAccount (paper fill model with measured spreads, start
equity 1000, same broker settings as the lab runs). A crash in one candidate's on_bar is caught, recorded as
an `error` event for that candidate, and does not stop the others.

### 2. Lanes
- `candidate`: stage `validated` or `shadow` in candidates.json.
- `rejected`: gate FAIL candidates keep running in a separate lane, so we learn whether rejecting them was
  right. They are never promoted from this lane without a new pre-registration.
- `benchmark`: BTC hold and the equal-weight basket, started at the same instant, same costs.

### 3. Priming (the T3.2b gap)
A1 needs 365+ days of history, A2 about 2.5 years of weekly panel. On start, each Scanner is primed by feeding
the monthly 1m kline history (the same files replay uses) through its **own** LoopCore up to the last
complete minute, labelled `REPLAY/priming`, with the account reset afterwards to 1000 and **no signal recorded**.
The forward record starts at the first live instant (`forward_start`). Priming is deterministic, so a restart
reproduces the same strategy state.

### 4. The shadow chain
`data/local/shadow/<lane>/<strategy>/<YYYY-MM-DD>.jsonl`, append-only. Each record is a T6.1 envelope plus
`prev_hash` and `hash = sha256(canonical_json(record_without_hash) + prev_hash)`; the first record of a day
links to the last hash of the previous day (genesis = sha256("livr-shadow:" + strategy + forward_start)).
Record types: `shadow_signal` (written at the instant the signal is emitted, before the next open fills it),
`shadow_fill`, `shadow_mark` (daily equity mark at 00:00 UTC), `gap` and `error`.
A daily **chain head** (last hash per strategy) is written to `data/local/shadow/heads.jsonl`. Publishing the
heads outside the PC (for example, committing them to a repo daily) makes the record tamper-evident; that
needs owner approval and is not part of this design.

### 5. Gaps
When the PC sleeps or the stream drops, the gap is recorded (`gap` record: from, to, reason). Missed
instants are never back-filled into the forward record. On resume each Scanner is re-primed from history up to
the resume minute (state catches up), marked `resumed`; days with a gap are flagged in the scorecard and
excluded from forward statistics (disclosed, not hidden).

### 6. Forward scorer (daily, offline)
`python -m trading.shadow.score` reads the chain (verifying every hash first; a broken chain = strategy
`UNVERIFIED`, no verdict), rebuilds each account's daily equity from `shadow_mark`, and writes
`data/local/shadow/scorecards.json` per strategy: forward days (excluding gap days), signals, net return,
max drawdown, daily Sharpe, and the same gate as history (P0a family DSR with N = every trial logged, P0b
benchmarks over the same forward days). A forward PASS requires >= 60 clean forward days and >= 30 signals.

### 7. Terminal
The feed (T5.1) gains `shadow_signal` and `forward_scorecard` events and `GET /v1/candidates`
(cards + historical verdict + forward scorecard). The terminal's league panel (P3) shows them.

### 8. Where it runs
The PC, as a scheduled task `LIVR-Shadow` next to `LIVR-Recorder` (registered by the owner, like
`register-autostart.ps1`). Later the VPS (deploy plan B). CPU: daily/weekly strategies are cheap; the 1m
engine per candidate is the cost (~7 books today).

## Test plan
1. Two candidates in one fleet: signals of one never appear in the other's account.
2. Priming then live on recorded ticks equals one replay over the same span, from `forward_start` on.
3. Chain: altering any byte of any record makes `score` report UNVERIFIED; day rollover links correctly.
4. A gap produces a `gap` record, no back-filled signals, and the day is excluded from scorecards.
5. One plugin raising in on_bar: an `error` record; the other candidates continue.
6. No exchange client importable from `trading.shadow` (extend the existing import guard).

## Pre-mortem
- **Forward looks great, fills were fantasy.** Paper fills use measured spreads; real fills differ. Promotion to
  real money still needs its own design and the F3 `real.closed` comparison.
- **The PC sleeps often, forward days accrue slowly.** Gap days are excluded, so 60 clean days may take longer
  than 60 calendar days. Moving to the VPS fixes it.
- **Priming differs from replay.** Test 2 guards it; a mismatch blocks go-live.

## Decision needed from the owner
Approve this design so P2 can be built (new module `trading.shadow`, a `ShadowFleet` wrapper around the existing
Scanner, no change to order code or the ledger).
