# The paper book was trading the live book's disaster stop

**Date:** 2026-11-02 · autonomous improvement pass (cron)
**Scope:** `scripts/unified_trader.py` (paper opener), `scripts/live_executor.py`
(path constant only). No order path, no threshold that gates real money, no
disable list, no state file touched.

---

## 1. What was wrong

`data/exit-params.json` is written by `exit_calibrate.py` and carries its own
scope guard:

```json
"scope": "live-trades.json (dip-buy spot executor) ONLY",
"disaster_stop_pct": 0.025
```

`unified_trader._paper_open()` imported `live_executor._exit_params()` and used
that number as the **initial** stop of the paper book:

```python
stop = _exit_params()["stop_pct"]        # 0.025, a DISASTER stop
tp = round(close * (1 + stop * 2), 4)    # +5.00%
sl = round(close * (1 - stop), 4)        # -2.50%
```

Two different measurements wearing one name:

| | live dip-buy executor | paper opener |
|---|---|---|
| role of the stop | disaster / tail bound | initial stop |
| wants it | **wide** (plateau 2.5–5%) | **tight** (2% study) |
| RR | not derived | fixed 2:1 from the stop |

The paper book's own comment three lines above the call says the value is
**2%** from the 2026-09-16 paired split-half study (+1.896 IS / +1.811 OOS,
t=+3.58, 13/14 symbols). The code booked **2.5%**, and the target **5.0%**.

This got *worse* over time, silently: `exit_calibrate.py` runs weekly on
`live-trades.json` and the stop plateau is flat from 2.5% to 5%. Every step of
that calibration moved the paper book's stop and target too. The opener printed
prices, never the width or its source, so nothing on the card could show it.

**Measured on the live book (2026-11-02):** all 5 open positions carry a
+5.00% target, i.e. a 2.50% stop; 594 of 2727 collapsed closed records carry
`sl > entry` (a trail that had ratcheted), and 735 carry `sl == entry`
(breakeven lock) — so the width is not recoverable from the closed records
alone. The open book is the evidence.

## 2. Why it is a *lesson* and not a typo

SYSTEM_MAP #9/#13/#15 state the rule for a **measurement**: re-derive it at
read time, at the basis it was measured on. #18 states it for a **gate**: a gate
must name the population it protects. This is the same rule applied to a
**knob**:

> A parameter is a measurement. Reading it from a file whose scope is a
> different book is the same defect as quoting a stale mean.

The write side already knew. `exit_calibrate.py` emits `scope` and
`not_applicable_to`, and `SYSTEM_MAP` §7 lists it. **A scope field that no
reader enforces is a comment.** The guard was documentation, not code, and the
one reader that should have honoured it did not.

Corollary: the fix that *introduced* this was itself correct in intent. The
2026-09-20 note says "both tracks now read `data/exit-params.json`" — paper had
been widened 1% → 2% on a real study while live kept bleeding at 1%, and the
author wanted one source of truth. The mistake was assuming one file means one
meaning. **Sharing a file is not sharing a measurement.**

## 3. What shipped

`unified_trader.py`:

* `PAPER_INITIAL_STOP_PCT = 0.020`, `PAPER_RR = 2.0`, `PAPER_STOP_CAP = 0.030`
  — module-level, beside the other paper thresholds, each with the study it
  came from.
* `_paper_exit_params()` returns `{stop_pct, rr}` together, so the target is
  always `rr ×` the stop actually used. A separate RR constant is how 2:1
  quietly becomes 1.6:1.
* `_paper_open()` reads only that. The `live_executor` import is gone.
* The OPEN line now prints the width and its source:
  `PAPER OPEN ETH/USDT @ 2241.4900 TP 2331.1496 SL 2196.6602 (paper width 2.00%, RR 2.0)`

`live_executor.py`: `_EXIT_PARAMS_PATH` lifted to module scope so a test can
point it at a fixture instead of the real `data/` dir. **Behaviour unchanged** —
the exporter still reads `disaster_stop_pct`.

## 4. Evidence

Red first (`tests/test_paper_stop_basis.py`, 5 tests):

* Before: 4 failed — `_paper_exit_params` did not exist, and the structural
  test found `_exit_params` in the opener's call set
  (`old opener called _exit_params: True`, verified against `git show HEAD`).
* After: **5 passed**. Full suite **838 passed, 4 skipped, 0 failed**
  (baseline this pass: 833 / 4 / 0).

Mutation checks (each reverts a different half of the fix):

| mutation | caught by |
|---|---|
| `_paper_open` re-imports `live_executor._exit_params` | `test_opener_does_not_import_the_live_calibrator` |
| `_paper_exit_params` returns the live stop again | `test_paper_stop_does_not_track_the_live_calibrator` |

Reachability check (SYSTEM_MAP #16) — a dry run through the **real opener**
against a scratch book:

```
PAPER OPEN ETH/USDT @ 2241.4900 TP 2331.1496 SL 2196.6602 (paper width 2.00%, RR 2.0)
stop width booked: 2.00%
target distance  : 4.00%
RR               : 2.00
```

## 5. What this does NOT do

* **It does not rewrite history.** Trades booked at 2.5%/5.0% stay as booked.
  `sl` is not recoverable per-record (trails and breakeven locks overwrite it),
  so any "what if" on the closed book is a *replay* question, not a patch.
* **It does not touch the 5 open paper positions.** They keep the exits they
  were opened with — changing a live stop mid-trade would make the population
  unanalysable and is an owner decision, not a cron one.
* **It does not change the live executor.** Same number, same file, same
  behaviour.
* **It does not claim the old width was "wrong" in dollars.** Nobody measured
  2.0% vs 2.5% on the paper book *as the paper book trades it* (entry from the
  scanner's close, exits sampled at :16/:46, 2h time-stop). The 2026-09-16
  study measured 1.0/1.5/2.0 on the native entries and stopped at 2.0. So this
  restores *the measured value*, not a proven-better one.

## 6. The measurement this now makes possible (next pass)

The honest experiment is a paired A/B on the paper book itself, which the fix
just made expressible: hold entry/time-stop/scan cadence fixed, vary only
`PAPER_INITIAL_STOP_PCT` ∈ {1.5, 2.0, 2.5, 3.0}, RR held at 2.0, split-half
IS/OOS, shared account, fees at the measured 0.1515% round trip.

Until that runs, the 28-day live-edge PF of **0.76** (n=635, −4.76 net) stands
as the open finding it already was. This fix does not move it, and must not be
reported as if it did.

---

## 7. What the overnight shift measured on top of the fix

The fix is live (11/11 open positions now print `paper width 2.00%, RR 2.0`;
`logs/unified.log` confirms). This section is what could and could NOT be
concluded from the closed book afterwards. **Read it before quoting any
"2% vs 2.5%" number.**

### 7a. The 2.0% cohort is 2 days old and cannot be compared to the 2.5% one

Designed width is recoverable only from records whose stop was never trailed
(`sl < entry`); honest cost cohort (n=687, `cost > 0.0015`):

```text
width 2.00%  n= 37   all dated 2026-10-10/11    net  -1.0771  mean -0.0291
width 2.50%  n=302   dated 2026-10-10 -> 11-02  net -15.4851  mean -0.0513
```

There is essentially **no day-overlap**: on 10-10/10-11 the counts are 3 and 4.
The two cohorts differ by **era**, not only by stop. Comparing their means is
the "half-split found a crossing point" error with the split chosen by width
instead of by date. **Not quotable.**

### 7b. What IS measurable: the 2.5% stop was rarely the binding exit

Within the 2.5% cohort, sampled adverse extreme (`worst_px`) by exit reason
(n=289):

```text
dip <= -3%     SL 13
dip (-3,-2]    SL 27   TIME 15
dip (-2,-1]    SL  0   TIME 88
dip > -1%      SL  0   TIME 145  STALL 1
```

**No trade that stayed inside −2% ever hit the stop, and every trade that
passed −2.5% exited at SL — none recovered.** On this book the 2.5% stop is
either never reached (TIME closes it) or reached and terminal. A stop-width
change therefore acts only on the 40/289 that actually breached.

### 7c. Simulated widths — ordering only, levels refused

Re-pricing the 2.5% cohort at other initial stops (breach = lose `thr` + cost,
otherwise keep the recorded outcome):

```text
stop    breached    net      delta vs book
-0.75%  178/289   -9.6965   +5.8545
-1.00%  143/289  -11.0048   +4.5462
-1.25%  104/289  -11.6270   +3.9240
-1.50%   87/289  -12.3307   +3.2203
-2.00%   55/289  -13.1362   +2.4148
-2.50%   40/289  -13.8520   +1.6990
```

Two reasons this is not a result:

1. **The bias runs with the finding.** `worst_px` is sampled at the :16/:46
   scan cadence, so it is a **lower bound** on the true adverse extreme — a
   wick between samples is missed. Tighter stops book MORE real breaches than
   this shows, so the tightening advantage is overstated by an unknown amount.
2. **It does not reproduce its own baseline.** The −2.5% row gives −13.85
   against the book's −15.55, because the 40 recorded SL exits are proof of
   breaches the field under-counts. A simulation that cannot reproduce the
   baseline has validated nothing.

**Use the direction (tighter lost less on this cohort). Never the levels. Do
not change a threshold from this table.**

### 7d. The next step is now cheap, and it is not a stop sweep here

The decisive version is a replay over the DB's 5m bars at the live :16/:46
cadence — the harness the 2026-09-16 study already used — which needs no new
recording. That is the next pass's job. **This pass deliberately did not move
`PAPER_INITIAL_STOP_PCT` off 2.0%** on the strength of a scan-sampled,
optimistically-biased table, and the direction it points (tighter) is opposite
to the direction the live calibrator's own sweep points (wider) — exactly the
unresolved contradiction `trade-lesson-loop` §2 already records.

### 7e. Standing caveats unchanged

- 28-day live-edge PF **0.76** (n=635, −4.76 net) is untouched by this fix.
- The fix restores the *measured* value (2.0%); it does not prove 2.0% > 2.5%.
- Closed history is not rewritten; open positions keep their booked exits.
