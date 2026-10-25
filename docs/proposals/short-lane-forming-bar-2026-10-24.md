# The forward lane priced its entries off a candle that had not finished forming — 2026-10-24

**Status: FIXED.** `scripts/short_regime_paper.py`, `scripts/research_loop.py`,
two new test files. No live config, order path, parameter or disable list was
touched. Paper/report code only.

---

## 1. How it was found

The regime-gated short lane (`scripts/short_regime_paper.py`) was created at
10:16 UTC today, hours before this pass, to forward-test the one hypothesis in
the short-rule certification that a backtest cannot settle. It had run twice,
both times reporting `gate OPEN (BTC -3.3% vs SMA200) | opened 0`.

The first run recorded `latest_bar_ms = 1792814400000` — the 4h bar that OPENED
at 08:00 UTC. The run's own timestamp was 10:16 UTC. It was reading a bar that
was still forming.

## 2. Why that happens (the data layer, not the lane)

    ingest_all.py        cron :12/:27/:42/:57
    fetch_closed_candles requests raw limit=60 — the exchange's published array
                         INCLUDES the in-progress candle
    upsert_candle        INSERT OR IGNORE on (symbol, timeframe, open_time_utc)

The first write of a key wins. So the 4h row for 12:00 exists from ~12:15 with
fifteen minutes of data, and its open/high/low/close/volume never refresh for
the rest of that bar's life. `fetch_closed_candles` does not drop it — verified
directly against the live exchange:

    1792814400000  open 80977.92  high 81097.47  low 80696.0  close 80732.01
    1792828800000  open 80732.01  high 80828.91  low 80451.18 close 80599.41
    1792843200000  open 80599.41  high 80844.01  low 80553.84 close 80844.01  <- forming, vol 268 vs 2042/1374

Live values on this box: XRP/BNB/ADA/LINK/DOGE newest 4h row is 00:00 (19 h
stale, eight bars behind); BTC/ETH/SOL is 08:00, frozen at its 08:15 snapshot.

## 3. What it did to the forward record

The lane used `bar[-1]` for three different decisions:

1. **the gate** — BTC close vs its 200-bar SMA. Live reading now is −3.5%
   (`btc_px 80599.41`, `sma200 83517.72`). The band between the frozen 08:15
   print and the true 12:00 close is ±0.04%, and the gate flips on far less.
2. **the entry** — priced AT `bar[-1].close`. A trade opened "at" a price that
   was the market's position fifteen minutes into a bar is not a price that was
   fillable at decision time; and the stop/target walk then settles it against
   real later bars, so the P&L of every trade carries an entry that never
   existed.
3. **the rules** — `f_volbreak` / `f_bearcross` read the partial close, so a
   breakdown or cross can print on partial data and retract when the bar
   completes. Verified in a fixture: a forming bar under the 20-bar low on 50×
   volume fires `volbreak 20 x2.0`; the last *closed* bar does not.

The lane's own docstring argued the record "cannot be tuned to the forward
sample". That part is still true. It was simply not the record it claimed to be.

## 4. The fix

`last_closed(rows, tf, now)` drops every bar whose window has not ended:
`open_time + duration <= now`. The predicate is on the OPEN time, so it holds
regardless of how late the row was written or how frozen its values are, and it
is keyed on each bar's own window — not "is the newest row newer than its
predecessor", which would keep the next bar the exchange publishes the instant
the previous one closes.

Applied at all three read sites (gate, entry, settle). Entries are one bar
later than they were; that is the cost of the entry being a price that existed.

A `meta.seams` entry records, once, that everything before this mark was
computed on unfiltered bars. Nothing is deleted — the lane had zero closed
trades, so no number was affected — but a later reader can tell which era a
trade came from.

**Not fixed here (owner-visible):** `ingest_all.py` writes forming bars for every
symbol and timeframe, and gates the 4h and 1d decisions of the live scanner the
same way. Fixing the ingest is a change to the data path that many scripts read,
so it is escalated rather than done in this pass. Until then every consumer must
filter, and the filter is now a named helper with tests.

## 5. Companion defect: research_loop contradicted itself in one message

Found while re-running the nightly research loop to see whether today's short
certification changed its verdict. The loop now emits, correctly:

    LIVE EDGE DECAYED: sma_r=150 now median 27.08% (4/4) — ABOVE its own
    leave-one-coin-out ceiling +23.48pp — regime break, not decay

and from the same run, in the same message:

    REFUSED 'NEW BEST' sma_r=100 (median 33.52%, 4/4 folds): the lead over LIVE
    is one-coin luck — ... Not an edge; the incumbent stands.

`sma_r=100` is the config the loop currently crowns; `sma_r=150` is LIVE. The
first line says the incumbent is in a regime break, the second calls a different
config "the incumbent" and says it stands. A reader cannot tell which config
the system runs.

Root cause: `_change_lines` gates the NEW BEST branch on
`score(best) > score(prev_best)` — the right cutoff — but the *reason* it
computes is `lead_is_coin_fragile(best, live)`, and when the challenger IS the
incumbent, `lead = 0`, and 0 is inside any leave-one-coin-out band. The check
can only ever report "fragile" for the config compared with itself. `main()` in
this repo never hit it because `prev` is always the previous run and a new best
is rare; the contradiction needs a best that is both stable and equal to LIVE's
role — which the current cache has.

Fix: the cutoff is kept and made explicit (a config that has not OUT-scored the
previous best is not news; a tie is not news either, and a tie previously
admitted a refusal computed against a stale challenger). The decay branch is
untouched: it answers a different, still-meaningful question, and its direction
wording is already correct in both tails.

## 6. Evidence

    RED   tests/test_short_lane_closed_bar.py        5 failed (last_closed absent)
    GREEN same                                        6 passed
    mutation  last_closed -> return list(rows)        6 failed, file restored
    GREEN tests/test_research_loop_news_cutoff.py     5 passed
    mutation  cutoff -> if True:                       2 failed, file restored

(the research_loop tests were written after the fix; the RED evidence for them
is the mutation run, not a pre-fix run — stated plainly rather than dressed up)

    full suite  tests/            415 passed, 3 failed (pre-existing, see §7)
    loop suite  tests/loop        355 passed, 3 skipped

    E2E  short_regime_paper.py x2  gate OPEN (BTC -3.5% vs SMA200) |
                                   opened 0 | closed 0 | book: 0 closed, net +0.00
                                   one seam written, not one per run

## 7. Pre-existing failures, not from this change

`tests/test_data_health_coverage.py` (2) and
`tests/test_research_loop_coin_fragility.py::test_the_solo_challenger_is_measured_even_when_it_has_no_family`
(1) fail identically on a clean tree (`git stash` verified for the first two;
the third fails inside `_change_lines` when handed a one-row `prev`, a fixture
shape `main()` never produces). They are reported, not patched: each is a
different question from this pass, and patching a test to match today's cache
is the exact failure its own docstring warns about.

## 8. Lesson

A "closed candle" is a property of the CALENDAR, not of the table. Every layer
here — fetcher, store, reader — is individually reasonable, and together they
produce a row that looks closed, is newest, never changes, and is wrong. The
forward lane was three hours old and its second run was already pricing off a
bar that had not finished. The check that catches this is cheap and permanent:
for any decision priced off the newest row, assert `open_time + tf <= now`.
