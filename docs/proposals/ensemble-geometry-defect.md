# The ensemble forward book measures the wrong instrument

**Status: FINDING + PROPOSAL — 2026-10-09. No code changed. No book touched.**
Read-only analysis of `data/ensemble-paper.json` (46 closed) against
`scripts/ensemble_paper.py` and `data/strategy-book/ensembles.json`.

Follows `docs/proposals/ensemble-outside-lesson-loop.md` (2026-10-07), which put this
book inside the lesson loop. That doc read the book's 27/28 `TIME` collar as a
*strategy* signal ("momentum ensembles do not generalise"). **This doc shows the
collar is an INSTRUMENT defect: the target was unreachable by construction, so the
book cannot speak about the momentum rule at all.**

---

## 1. Verdict (one line)

**Every one of the 46 closed ensemble trades carried a target +3.00% away while the
best realised move ever recorded was +1.24% — the take-profit was mathematically
unreachable in 100% of trades (0 TP exits), so the forward book is measuring hold-time
noise, not the strategy it was built to test. A second defect collapses each
multi-symbol ensemble to a single symbol, leaving 13 of 26 members in the 30m group
unmeasured.**

---

## 2. Defect 1 — the target is unreachable by construction

`scripts/ensemble_paper.py:194-196` hardcodes the stop and derives the target:

```python
stop_pct = 0.02                                  # fixed 2% stop
tp = round(entry * (1 + stop_pct * rr), 6)       # rr from the member id
sl = round(entry * (1 - stop_pct), 6)
```

With `rr = 1.5` on every traded member, **TP = entry × 1.03 → +3.00%, SL → −2.00%.**
The hold is `hold_bars` (48 bars × 5m = 4h, or 12 × 30m = 6h). Measured over the book:

```text
TP distance %   : min 3.00  max 3.00  median 3.00     <- identical on all 46
realized move % : best +1.235  worst −2.000  median +0.028
TP exits        : 0 of 46
exit mix        : TIME 43 · SL 3
```

**The best move any trade ever achieved (+1.24%) was less than half the required
target (+3.00%), and no trade came within 1.75 percentage points of its target.** In
4–6 hours BTC essentially never travels 3%, so the geometry guarantees a `TIME` exit.

### Why this matters — it reinterprets the 2026-10-07 conclusion

That doc read `TIME 27/28` as the *wide-stop mechanism* from `trade-lesson-loop` §2
("the exit machinery never fires"). The mechanism is real, but here it is not the
strategy's doing: **the exit was made unreachable before entry.** The book's −1.04
net over 46 trades is a property of `stop_pct=0.02 × rr=1.5 × a 4–6h hold`, not of
the momentum rule.

⇒ **No conclusion about these ensembles is supported by this book.** The
100%-coherence backtest gate and the forward book are not in disagreement — the
forward instrument never tested the thing the gate approved.

---

## 3. Defect 2 — multi-symbol ensembles collapse to one symbol

`scripts/ensemble_paper.py:129-140` uses only the **first** member's symbol:

```python
parts = mids[0].split("|")
symbol = parts[1]      # <- the FIRST member's symbol, only
lb, hold, rr = int(parts[3]), int(parts[4]), float(parts[5])
```

But `strategy_compact.py:77` groups members by `(timeframe, family)` — **not by
symbol** — so a group genuinely spans instruments:

```text
ens|momentum|30m  members 26
  by symbol:  BTC/USDT 13 · SOL/USDT 11 · ETH/USDT 2
  first member traded: momentum|BTC/USDT|30m|12|12|1.5
```

**The book traded BTC only.** The 11 SOL members and 2 ETH members — 13 of 26, half
the group — have never been measured. The ensemble's stated premise ("members vote
across instruments") is silently discarded by the runner.

Same for the third ensemble: `ens|channel_breakout|4h` (ETH/XRP/DOGE, rr 3.0) trades
its first member ETH — and has **0 closes**, though the runner's own docstring names
it as one of "the two live ensembles."

---

## 4. What is NOT claimed

- **Not claimed:** the momentum ensembles are bad. They are **untested** by this book.
  The backtest gate's 100% coherence is neither confirmed nor refuted here.
- **Not claimed:** the fix will make the book profitable. A reachable target may
  simply produce honest losses — that would be *information*, which is the point.
- **Not claimed:** the ensemble design is wrong. Groups-spanning-symbols is a
  defensible ensemble. The defect is that the runner does not implement it.
- **Not claimed:** the 3 SL exits are strategy stops. With a +3% TP / −2% SL and a
  tiny realised move, the book never exercised the rule as designed at all.

---

## 5. Proposed fix (for review — NOT applied)

Two options for Defect 1; the second is preferred as it matches the members' own
recorded geometry:

1. **Shrink `stop_pct`** so the target is reachable in the hold window (e.g. match the
   book's tight-stop regime <1.5%).
2. **(Preferred)** Read `stop_pct` from the member's own recorded entry in
   `entries.jsonl` instead of a hardcoded 0.02 — the backtest gate that approved
   these members already used a stop; the paper runner should use the same one, or it
   is testing a different strategy than the gate passed.

For Defect 2: iterate **all distinct symbols in `member_ids`**, opening one position
per symbol (the existing `MAX_OPEN_PER_ENS=3` cap and `(ensemble, symbol)` de-dupe
already support this — they were written for it).

Both changes touch **only `ensemble_paper.py`**, which writes only its own paper book;
no live state, no order path. Any change should be validated by replaying the runner
over the recorded candle window before going forward.

---

## 6. Verification performed

- All 46 closed trades read from `data/ensemble-paper.json`; TP distance recomputed
  from `entry`/`tp` (min = max = median = 3.00%). Best realised move from recorded
  `exit`. **0 TP exits.**
- Member symbols read from `data/strategy-book/ensembles.json` `member_ids`; first
  member confirmed as the traded symbol in every book record (`ensemble` field).
- Runner logic read from `scripts/ensemble_paper.py:129-205`; grouping logic read from
  `scripts/strategy_compact.py:75-108`.
- **No file written except this document.** The book was not re-run by hand; the
  lesson loop was verified up to date (2771 lessons, 0 new).

---

## 7. Generalisable lesson

**A forward paper book can be a faulty instrument, and its failure shape looks like a
strategy result.** The 2026-10-07 proposal did exactly the right thing (wire the book
into the loop) but read the collar as evidence about the strategy. Before drawing any
conclusion from a forward book, check that the instrument could have recorded the
outcome it is being judged on: **if no trade ever reached its target, the target was
the problem, not the strategy.** Same family as `trade-lesson-loop`'s *"a constant YOU
typed into the analysis is not evidence about the system"* — here the typed constant is
`stop_pct = 0.02`, and it decided every trade's outcome before the rule fired.
