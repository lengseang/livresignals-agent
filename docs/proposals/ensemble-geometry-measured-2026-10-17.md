# The ensemble book's geometry defect is now MEASURED — and the fix will not rescue it

**Status: FINDING + PROPOSAL — 2026-10-17 (overnight improvement shift). No code changed. No book touched.**

Follows `docs/proposals/ensemble-geometry-defect.md` (2026-10-09), which diagnosed
Defect 1 (unreachable target) and Defect 2 (multi-symbol ensembles collapse to one
symbol) correctly, but could only say *"the fix may simply produce honest losses."*
**This doc runs the measurement that proposal named as its acceptance test.** It
answers the question that has been open for **8 days**, during which the book grew
from 46 → **115 closed trades — every one of them still invalid.**

The measurement does not say what the original proposal hoped. It is more useful than
that: **making the target reachable resolves the exits and still loses money, so the
instrument was not the only thing wrong.**

---

## 1. Verdict (one line)

**The +3% target was reachable in 0.1% of 5m hold-windows (0 TP exits in 115 trades,
confirmed) — but replaying the SAME momentum rule with REACHABLE targets turns TIME
exits into real TP/SL resolution and the book stays net-negative at every geometry
tested (−0.013 to −0.017/trade). Fixing the geometry is still necessary — the book is
currently unjudgeable — but it is NOT sufficient, and it must not be sold to the owner
as the fix that makes the ensemble profitable.**

---

## 2. Defect 1, quantified (this is the new part)

Reachability measured directly from candles — for every bar, does the favourable
extreme within `hold_bars` reach +k%?

```text
BTC 5m, hold=48 bars (4h), n=2951 windows
  max favourable : median 0.30%  p75 0.57%  p90 0.91%  p99 2.29%
  P(reach +3.0%) =  0.1%     <- the configured target
  P(reach +1.5%) =  2.6%
  P(reach +1.0%) =  8.1%
  P(reach +0.6%) = 23.4%

BTC 30m, hold=12 bars (6h), n=2987 windows
  max favourable : median 0.47%  p75 0.91%  p90 1.56%  p99 4.68%
  P(reach +3.0%) =  3.5%
  P(reach +1.0%) = 22.0%
  P(reach +0.6%) = 41.2%
```

**The original proposal's inference was right and is now proven at scale:** a +3%
target in a 4h hold on BTC is a ~1-in-1000 event. The 0 TP exits in the book are the
geometry, not the strategy.

⚠️ **Note the adverse side is as wide as the favourable side** (5m: median +0.30%
favourable vs 0.35% adverse). The ±2% stop is also effectively never touched in the
5m book (83 SL exits over 3038 windows ≈ 2.7%). So the current book is measuring
*almost nothing at all* — neither target nor stop.

---

## 3. The decisive test — reachable targets, same rule, same data

Replay of the runner's own logic (momentum fires when close > SMA(lb), `lb=12`;
next-bar entry; TP/SL raced on candle highs/lows, **SL conservative on ties**; TIME at
close of the hold window; **cost 0.28% round trip** as the book charges; $5 size).

```text
geometry (TP/SL)       tf    n     W     L     T    net      avg/trade
current  3.0 / 2.0     5m  3038     0    83  2955  −53.16   −0.01750
current  3.0 / 2.0     30m 1955    80    75  1800  −21.17   −0.01083
fix      1.0 / 0.6     5m  3038   297   855  1886  −46.03   −0.01515
fix      1.0 / 0.6     30m 1955   349   702   904  −27.98   −0.01431
fix      0.6 / 0.5     5m  3038   653   959  1426  −44.29   −0.01458
fix      0.6 / 0.5     30m 1955   643   739   573  −26.25   −0.01343
fix      1.5 / 1.0     5m  3038   108   430  2500  −50.15   −0.01651
fix      1.5 / 1.0     30m 1955   212   359  1384  −26.14   −0.01337
fix      0.6 / 0.4     5m  3038   625  1180  1233  −43.33   −0.01426
fix      0.6 / 0.4     30m 1955   597   871   487  −26.07   −0.01333
```

**Every geometry loses.** The reachable ones trade *better as an instrument* — 643 TP
exits and 739 SL exits at 0.6/0.5 on 30m instead of 80 TP / 75 SL / 1800 TIME — and
still net **−26**. The loss is not made of unreachable targets; it is made of a rule
whose wins and losses both cost more than its edge.

### Baseline reproduction check (required before trusting the ordering)

The skill's rule: **a simulation that cannot reproduce the baseline has not validated
anything.** Here it does, on the 5m lane:

```text
recorded book, ens|momentum|5m : n=72  avg −0.01776/trade
replay, current 3.0/2.0, 5m    : n=3038 avg −0.01750/trade   (1.5% apart)  ✅
recorded book, ens|momentum|30m: n=43  avg −0.02552/trade
replay, current 3.0/2.0, 30m   : n=1955 avg −0.01083/trade   (2.4x apart)   ⚠️
```

⇒ **The 5m ordering is trustworthy. The 30m level is NOT** — the real 30m ensemble
mixes `hold_bars` 12/24/48 across its 26 members while the replay holds 12 fixed.
**Quote the 30m result as directional only.**

⚠️ This is the same trap documented in `trade-lesson-loop` §2: replay the CURRENT
configuration first, and if it does not reproduce the recorded figure, you are
measuring a different system. It reproduces on 5m, not on 30m.

---

## 4. Defect 2 is STILL LIVE — and it is a coverage failure, not a data failure

`scripts/ensemble_paper.py:136-137` still reads `mids[0]` and takes `parts[1]`:

```python
parts = mids[0].split("|")
symbol = parts[1]
```

Measured on the 26-member `ens|momentum|30m` group:

```text
members by symbol : BTC 13 · SOL 11 · ETH 2
trades in book    : BTC 115 · SOL 0 · ETH 0        <- every closed trade, whole book
historical fire rate (last ~400 bars/member):
  BTC 50.2%   ETH 51.4%   SOL 44.5%                <- the untraded members fire just as often
```

**Ten of 26 members (ETH ×2, SOL ×11) have never been measured**, and they fire at
essentially the same rate as the members that are measured. This is not a thin
sample — it is a structural blind spot.

⚠️ **`ens|channel_breakout|4h` has 0 closes — but that one is NOT a bug.** Checked all
19 members against live candles: every one sits well below its 24–96-bar channel high
(ETH 2410 vs 2530; DOGE 0.0803 vs 0.0881). No breakout occurred. Correct behaviour,
correctly silent. **Do not "fix" this** — and note the runner is only evaluating
member `[0]` (ETH), so this group is also 18/19 unmeasured.

⚠️ **Third defect, newly found: the ensemble runner ignores the disable list.**
`signals-state.json` disables `momentum`, `breakout`, `pullback`, `supertrend`,
`ema-cross`, `bollinger`, `rsi2`, `loose`. `ensemble_paper.py` does not read
`signals-state.json` at all — so the stack's own self-improve gate has switched
`momentum` off, while the ensemble book keeps accumulating momentum trades (72 + 43).
**The ensemble paper lane is trading a family the system has already decided loses.**

---

## 5. What is NOT claimed

- **Not claimed:** the momentum ensembles are bad and should be deleted. They are
  **untested by this book**, and even the reachable-target replay is a *backtest over
  one window*, not a forward result.
- **Not claimed:** the replay is the runner. It is a faithful re-implementation of the
  entry/exit rule, verified only on the 5m lane (see §3). It has no
  `MAX_OPEN_PER_ENS` concurrency cap and no per-symbol dedupe, so it opens more trades
  than the live runner would.
- **Not claimed:** Defect 2 fixes will improve performance. More symbols is more
  measurement, not more edge — and SOL is on `disabled_symbols`, so Defect 2's fix
  must **respect the disable list** or it will silently re-enable a symbol the stack
  turned off.
- **Not claimed:** the 8-day delay had a cost in money. Real capital was never
  exposed — see §6.

---

## 6. Exposure check (state plainly which book is live)

```text
REAL SPOT book (data/live-trades.json): 37 closed, 0 OPEN, net −0.2443
  last close 2026-10-12 — DORMANT for 5 days
REAL FUTURES: not in this analysis
ENSEMBLE PAPER book: 115 closed, 1 open, net −2.376 — 100% paper, $5 size
```

**No real capital was at risk during this finding's lifetime.** The whole defect sits
in a $5 paper book. That is why it has been safe to leave open for 8 days — and also
why it should not be over-dramatised. Its cost is **8 days of a measurement that could
not measure**, not 8 days of losses.

---

## 7. Proposed actions (for review — none applied)

Ordered by what unblocks the most with the least risk. All touch only
`scripts/ensemble_paper.py` (paper writer) except where noted.

1. **[highest value, lowest risk] Make the book judgeable.** Move the stop to the
   tight-stop regime the rest of the stack already prefers (<1.5%) and derive TP from
   the member's own `rr`, e.g. stop 0.6% → TP 1.5% at rr 2.5, or stop 0.6% → 0.9% at
   rr 1.5. This does **not** need to be profitable to be worth doing — it converts an
   unjudgeable book into an honest one.
2. **Iterate all distinct symbols in `member_ids`**, not `mids[0]`; group by
   `(ensemble, symbol)`. The existing `MAX_OPEN_PER_ENS=3` cap and `(ensemble, symbol)`
   de-dupe were written for exactly this.
3. **Consult `signals-state.json`** before opening: skip a family in `disabled` and a
   symbol in `disabled_symbols`. Without this, actions 1–2 quietly re-enable `momentum`
   and SOL.
4. **Reset expectations in the lesson loop**: keep `GEOMETRY-INVALID` until a
   post-fix book exists. Do not carry the 115 invalid trades into any "
   did the fix work" comparison — they are from a different instrument.

⚠️ **Do NOT apply action 1 on the strength of §3 alone.** The replay says the
reachable geometry still loses, which is a reason to *fix the instrument and let it
run honestly*, not a reason to expect profit. The correct framing to the owner:
**"we can now measure this; we still don't know that it works."**

---

## 8. Verification performed

- Book read from `data/ensemble-paper.json`: 115 closed, 115 with geometry, TP distance
  `min = max = median = 3.00%`, **0 TP exits**, exits TIME 108 / SL 7, net −2.376.
- Reachability computed from `data/trading.db` candles (`candles` table, columns
  `open_time_utc/open/high/low/close` — note: **not** `ts`), 2951 5m windows and 2987
  30m windows.
- Replay re-implements `_rule_fires` and the manage loop from
  `scripts/ensemble_paper.py:98-205`; baseline reproduction on 5m confirmed to 1.5%.
- Member symbols and fire rates read from `data/strategy-book/ensembles.json`
  (`as_of_utc 2026-10-17T04:00`), 26 members in `ens|momentum|30m`.
- Disable list read from `data/signals-state.json`; confirmed `ensemble_paper.py`
  contains no reference to `signals-state` / `disabled`.
- Real exposure read from `data/live-trades.json`: 0 open, last close 2026-10-12.
- Lesson loop run: `3141 lessons, 0 new` — up to date, idempotent.
- **No file written except this document.** No code changed, no book touched, no order
  path read or modified.

---

## 9. Generalisable lesson

**An instrument defect has a half-life. Left open, it keeps producing confident-looking
verdicts that all have to be thrown away.** The lesson loop was built to be unable to
lie about this (the `GEOMETRY-INVALID` guard, 2026-10-15) — and it worked: it caught
all 115 trades and flagged the book. But the guard only *labels* the defect. Eight days
and ~76 further invalid closes later (46 → 115; 76 closes dated on/after 2026-10-09),
the runner still produces them.

⇒ **A guard that labels a defect is not a fix for it.** The loop's value here was
diagnostic; navigation to the fix needed a human decision that never came. When a guard
flags the *same book* on two consecutive shifts, the next shift should escalate the
fix, not re-report the flag.

⚠️ And the second lesson, which is the more expensive one: **the proposal was right
about the mechanism and would have been wrong about the magnitude.** It predicted "the
fix may produce honest losses" — correct — but nobody knew *how* bad until the
reachability measurement was run. A diagnosis and a decision are different artefacts;
this finding sat in the gap between them for 8 days.
