# Learning: one dip was booked once per timeframe — 498 phantom trades in the paper ledger

**Date:** 2026-10-15
**Cron class:** LEARNING
**Commit:** `paper-book: collapse multi-timeframe duplicate fills in the learning ledger`

## What was found

`scripts/unified_trader.py` `_paper_open()` guards re-entry with

```python
if any(p["symbol"] == sig["symbol"] and p.get("tf") == sig["tf"] for p in s["open"]):
    return
```

One open position per `(symbol, timeframe)`. But `main()` scans the SAME price
feed once per timeframe (`_ordered_tfs()` → 5m / 15m / 30m) and calls
`_features()` on each, so a single physical dip produces a signal on **every
timeframe that happens to agree**. Each is opened, each closes on its own, and
each is appended to `closed`. One fill becomes two or three ledger rows.

Measured from `data/unified-paper.json` (2026-10-15):

```
closed records                                       2964
pairs <=5s apart, IDENTICAL entry AND pnl             498
  of those, DIFFERENT tf                              476
  of those, SAME tf (1.5-2.5 ms apart)                 22
across all 498 pairs:
  identical reason      498/498
  identical pnl_net     498/498
  identical worst_px    498/498
  identical tp          484/498
  identical sl          489/498
```

Two rows sharing an exact entry price, an exact dollar pnl, an exact exit
reason and an exact adverse extreme, seconds apart, are not two trades. The 22
same-tf pairs are 1.5–2.5 **milliseconds** apart — a true double-write inside
one scan; the 476 cross-tf pairs are the same fill wearing three timeframes.

## Why the 2026-10-14 `dedupe` fix could not catch this

`self_improve.dedupe` keys on `symbol + open time + entry + size` and removes
only **byte-identical** re-bookings. The multi-tf copies differ in `ts_utc`
(sub-second) and, on 14 pairs, in `entry` by one tick — so each copy is a
*different key* and **all survive**. The 2026-10-14 fix correctly stopped
merging two distinct trades that happened to share a price; it did not address
a population that is one trade wearing several timestamps. The two defects are
opposite errors (over-merge vs under-merge) and both live in the same ledger.

## Impact — the reported book was flattered by 17% of its rows

```
                                  RAW        COLLAPSED
records                          2964          2466        (-498, -17%)
gross pnl                      +18.995       +13.987        (phantom +5.01)
net pnl (0.16% round trip)      -2.485        -3.923
EV / trade                    -0.00084      -0.00159
```

The duplicates were **disproportionately winners** (they were re-counted TP
hits), so collapsing makes the book read **worse, not better** — the opposite of
the 2026-10-14 finding, where the buggy key hid trades. Every per-edge sample
shrank:

```
edge          raw n   collapsed n   raw net    collapsed net
loose          1684        1376      +6.595       +4.439
validated       918         818      -3.248       -2.698
supertrend      191         134      -2.143       -1.844
rsi2             27           7      -0.256       -0.238   <- now BELOW the n>=15 floor
```

`supertrend` lost 30% of its sample; `rsi2` fell from a judgeable 27 trades to
7 — **below the n>=15 floor, so it can no longer be judged at all**. Every
"DECAYING edge" magnitude in the prior reports was computed on a union of
trades counted twice.

## The fix — read-side, deliberately

New module `scripts/paper_book.py`:

```python
collapse_multitf(records, window_s=5.0) -> (collapsed, removed)
```

walks each symbol in open-time order and keeps the FIRST record of a run whose
members share **exact entry + exact pnl** within `window_s`. It is wired into
`self_improve.py` and `improvement_loop.py` immediately after `dedupe`, and the
removed count is surfaced on both the card and `reports/improvement-report.json`
(`edge_meta.multitf_collapsed`) so a shrinking book is never silent.

**Not changed:** the writer still opens one paper position per agreeing
timeframe. Whether one dip should consume three of the five paper slots is a
strategy-design question, not a measurement bug, and is out of scope here. This
change only stops the *learned statistics* from double-counting.

### Precision over recall

The key is strict on purpose: **exact** entry, **exact** pnl, both timestamps
present, within 5s. A test (`test_same_time_same_pnl_different_entry_is_not_a_copy`)
pins that a one-tick-different entry with a different pnl is kept as two trades.
Records without a parseable timestamp are **never** collapsed — measured
2026-10-15 when the first cut footgunned `tests/test_improvement_net_basis.py`:
its 20-record fixture carries no `ts_utc`, so a timestamp-optional key collapsed
it to 1 and tripped the `wait_for_data` floor. That test is now the guard for
the contract.

## Verification

- `tests/test_paper_book_multitf.py` — 8 tests: three-timeframe copy collapses
  to one (earliest ts kept); sub-second same-tf double-write collapses; distinct
  same-price trades hours apart are kept; same-time different-pnl is kept; one-tick
  different entry with different pnl is kept; untimed records are kept; the window
  is respected.
- A/B against a no-op collapse: **baseline 2 failed / 6 passed → with-fix 8
  passed**. The 2 baseline failures are exactly the collapse tests, proving they
  pin the behaviour, not the source string.
- Full suite: **615 passed, 3 skipped**, 0 failures.
- Live re-read: `paper n=2964 → 2466`, `net -2.485 → -3.923`,
  `gross +18.995 → +13.987`.

## What is NOT claimed

- This does **not** make the paper book profitable. It is still a NET loser
  (−3.923, EV −0.00159/trade) — it is a *worse* loser than reported, because the
  duplicates flattered it.
- It does **not** re-enable `rsi2`. It only makes the sample the gate sees
  honest (7, not 27) — which pushes it *further* below the floor. Any re-enable
  is a separate, reviewed change to `data/signals-state.json`.
- It does **not** touch the live book (real money): `live-trades.json` has 0
  such pairs, and `futures-trades.json` has 0. This is a learning-ledger defect.

## The generalisable lesson

**A dedupe key built on time cannot merge rows that differ in time.** When the
same event is written once per *dimension* (here, timeframe), every copy gets a
fresh timestamp and no identity key can see them as one — the only handle is the
*economic* fingerprint (exact entry + exact outcome). This is the sibling of the
2026-10-14 lesson (`a key is only as good as its presence on every record`): a
key is also only as good as its **discriminating power over the population it is
applied to**. Before trusting a book count, ask which *write pattern* could
duplicate a row, and key on the fields that survive that pattern — not on the
convenient identity the schema happens to expose.
