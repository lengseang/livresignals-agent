# Proposal 2026-11-05 (overnight shift) · the real-money book is reported on its PAPER log, and the real record contradicts it

**Status:** finding + proposed reader fix. **Nothing in the money path was changed.**
`scripts/expectancy_vs_fee.py` and `scripts/fee_audit.py` were **not edited** this shift —
both are on the loop's escalate list (they are the readers behind owner-facing numbers,
and `fee_audit.py` is a scheduled 09:50 job). This document is the design that task
T0.10 already calls for, with the numbers T0.10 was written before anyone measured.

## 1. What is printed now

`scripts/expectancy_vs_fee.py` — run this shift, unchanged:

```text
trend_follower (REAL $)   206   0   +1.451%   +1.291%   t=+1.98  21.8%  alive, unproven
```

The `206` is `trend-state.json` → **`log`**, a 53-symbol PAPER scan log. The record for
real money is `trend-state.json` → **`real.closed`**. The planner's own notes say so
(`docs/loop/designs/T0.5.md` §4), and `check-books.py` §5 separates the two shapes by
design. The row label claims REAL $ and the data is paper. This is T0.10's first bullet,
still open.

`scripts/fee_audit.py` (09:50 daily) does the same read and multiplies the log by a
**hardcoded `stake = 5.0`** — a number the real records contradict (below).

## 2. What the real record says (measured this shift, read-only)

```text
data/trend-state.json real.closed        n = 18
  pnl_usd present                        0 of 18   (all null)
  fee_status UNKNOWN                    18 of 18
  entry_status FILL 15 / BOOKED 3
  exit_status  FILL 15 / TRADE_HISTORY 3
  exit - entry mean return             +6.19%
  median                               -1.03%
  t                                     +0.97
  win rate                              22.2%   (matches the log's 21.8%)
  WITHOUT NEAR/USDT (one record)       +0.09%, n=17, t=+0.05
  exit-implied notional, median per rec  $5.49 ; sum $95.61
  gross PnL summed from prices         +$5.776
  fee actually paid (BNB, fee_raw)     0.00010317 BNB ≈ $0.071 at $690.4/BNB
  fee implied by 0.10%/side assumption   ≈ $0.191
```

Three facts follow, and each one alone forbids the current row:

1. **The real book's headline is one trade.** `NEAR/USDT` closed `TRADE_HISTORY` at
   `entry 2.369 -> exit 4.971` = **+109.8%** on a record whose `entry_status` is `BOOKED`
   (a pre-existing position, not one this book opened). Drop it and 17 records read
   **+0.09% mean, t=+0.05** — indistinguishable from zero, i.e. the same verdict the
   diagnosis prints for `futures_executor` with the sign flipped.
   Same shape as the published-ledger case (`demo-0004-NEARUSDT`): a track record whose
   sign flips on one trade is not a track record.
2. **The 0.1594% round-trip fee is an order of magnitude too big for this book.** It was
   measured on high-turnover fills (`fee_audit.py` `per_side`), while the real trend book
   pays BNB fees whose `fee_raw` totals **$0.07 across 18 closes**, against ~$95 of
   notional. At that rate the fee is ≈0.07% RT, not 0.159%. The fee is still a real cost
   and the *direction* of the conclusion does not change — but a net figure built on a
   2x-wrong fee is not a net figure. **UNKNOWN is the honest value until `fee_usd` is
   resolved per record.**
3. **The real book has been idle since 2026-10-29** (`NEAR` close). `real.open` is empty.
   The row's verdict is printed as though the book were running.

## 3. What is proposed (not done)

Per `designs/T0.5.md` §4, in one PR, readers only:

- `expectancy_vs_fee.py`: relabel the `log` row **`trend_follower (paper scan log)`**; add
  `real.closed` as `trend_follower (REAL $, from real.closed)` reading the **price-derived**
  return (`entry`,`exit`), printing
  `n_unknown` for `pnl_usd`/`fee_usd` next to any total. **Never average the two rows.**
- `fee_audit.py`: compute the "real money" block from `real.closed`; delete `stake = 5.0`;
  print fee from `fee_raw` (BNB x price), and `"fee/side UNKNOWN"` when absent — not a
  measured rate borrowed from a different cohort. Empty `real.closed` prints
  `"no real closes recorded yet"`.
- `book_units.py`: add a `real` row so `check-books.py` §1 covers this book too. It is
  **currently in none of the four `CASES`** — `trend-state.json` is checked only for
  freshness/shape/monotonic-on-`log`. The paper log gets a unit check; the real money
  does not.
- A one-record dependency guard: any book whose headline flips sign when its single
  largest record is removed must print `ONE-RECORD DEPENDENCY (n=1)` beside the number.

## 4. Test plan (for the PR that implements it)

1. `real.closed` with 3 records, `pnl_usd` null -> row prints from prices, `unknown=3`.
2. `real.closed` empty -> `"no real closes recorded yet"`, not `0.00%`.
3. Fee block: `fee_raw` BNB present -> sums at the given BNB price; absent -> `UNKNOWN`.
4. Remove-the-top-record guard fires on a fixture where one record carries the mean.
5. `check-books.py` unit case for `trend-state.json#real.closed` fails on a corrupted
   record (mutation: swap a return for its dollar amount).

## 5. What was deliberately NOT done, and why

- No edit to either reader: owner-facing numbers, and one of them is scheduled.
- No edit to `trend_follower.py` (`real.closed` `pnl_usd`/`fee_usd` write path) — real-money
  script, escalate list, and the T8.1 design is unapproved.
- No change to the paper book or any live parameter.
- **The real book itself is not being fixed by a reader.** The reader fix makes the
  printed number honest; it does not make the book profitable (17 of 18 records are
  noise around zero, and the fees are small here so the issue is the *signal*, not the toll).

## Pre-mortem: it is three months later and this failed. Why?

- **Two rows drift into one total again.** Mitigation: the reader returns a list of rows
  with a `basis` field; nothing sums across `basis` (book_units.tsv-style contract).
- **Someone re-derives a `stake` literal.** Mitigation: the unit test asserts the string
  `stake` does not appear in `fee_audit.py`.
- **The one-record guard fires so often it is ignored.** Mitigation: it is a printed
  annotation, not an alert; it appears on the row, every run, only for books where the
  flip is real (fixture-pinned).
