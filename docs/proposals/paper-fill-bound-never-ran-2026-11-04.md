# The paper-fill audit's bar method never ran, and the bound has no window

**2026-11-04 · overnight improvement shift (second pass).** Read-only on every
book, parameter, order path and state file. One commit touches a research script
and its tests. Nothing here changes a fill, a stop, a size, a book or a config
value; no report that has already been published is corrected by this, because
the method it critiques produced none of them.

The object is `scripts/paper_exit_fill.py`, written by the 2026-11-04 daytime
shift to answer one question: **is the paper book's net basis an upper bound or a
lower one?** Its own header says the answer is a one-sided bound on how far
adverse the unknown SL fill can be. This pass found three things about it.

## 1. The bar method returned `None` on every fixture that reached the stop

`summarise()` reads the in-force gain once per stop level and **caches the whole
`(mean, n)` tuple**, then hands `g[0]` — the mean — into the bar method:

```python
g = gain_cache[key] = avg_gain_in_force(recs, sp, width=0.25)   # -> (mean, n)
...
adv = adversarial_pnl(r, ts, b, g[0], tf_min, tau2_over_3)
```

That seam is textually correct today, and nothing verified it: **every fixture in
the test file passed a bare float**, and `test_summarise_is_one_sided…`
monkeypatches `adversarial_pnl` out entirely, so no test ever called the bar
method the way `summarise` calls it. The type is unenforced on both sides — the
next edit that passes `g` instead of `g[0]` is a live `TypeError` with a green
suite, exactly the shape the pre-`g[0]` draft had.

Measured on the shipped file before any edit:

```text
pytest tests/test_paper_exit_fill.py -q
1 failed, 12 passed, 1 xfailed
  test_adversarial_pnl_charges_the_worst_bar_through_the_stop
  TypeError: cannot unpack non-iterable NoneType object
```

That one failure is the whole bar method: **`adversarial_pnl` returned `None` on
every synthetic bar that reached the stop.** Not a tuple problem — a rule
problem, section 3.

## 2. `avg_gain_in_force` returns a NEGATIVE gain on every stop level of the live book

The bound is built on "the average edge in force", i.e. the mean booked profit of
closed trades whose own stop sits within 0.25pp of this one. Measured on the
3,568-record book:

```text
stop level   mean booked profit (n)
-1.00%        -0.52%   (421)
-1.20%        -0.52%   (417)
-2.00%        -0.94%   (768)
-2.50%        -0.92%   (495)
```

**Every level is negative.** The population it describes is "trades that closed
at about this stop", i.e. mostly losses — the average is not "the edge in force"
for those trades, it is their outcome. The docstring's reading of the sign is
therefore wrong by construction, and `g * …` with a negative `g` would flip the
sign of the bound in `_one_bar_adverse_bound`, or, in `adversarial_pnl`, silently
produce `None` through the loop's arithmetic.

This is not repairable by clamping to `abs()`. The quantity the method needs is
the profit of a trade **still open** at a bar; the only proxy in the book is
closed outcomes, selected on the stop they exited at — a conditioning that
guarantees a loss. **The input does not exist in this dataset.** The honest
statement is: the bar method cannot be computed from `unified-paper.json`.

## 3. The bar method's window is the whole future

Independent of the sign: `adversarial_pnl` walks **every bar from entry to the
end of the series**, not to the record's own close time. A position that reaches
its stop band gets the worst fill anywhere in the remaining history, including
bars after it was actually closed. The `summarise` docstring already says this
out loud and calls the `min(adv, booked)` clamp a "conservative floor, not a
validated bound". They are right; the clamp is the only thing keeping the
function from reporting a position worse than any trade could have been.

`bracket_vs_level` — the part that does not need bars — is sound and is what
should be quoted. Measured:

```text
SL records with a level      1,332
  booked beyond their level  1,269   (inside: 0)
  exactly at their level        63
mean gap vs level            -0.52pp
below-entry  n=526  beyond=517  mean_gap -0.62pp
```

**98.3% of below-entry SL records book beyond their own constant stop, none
inside it.** That is a one-directional stamp and it is the citable finding: the
booked SL fill is *not* the stop price, and it is never better than it. The
writer's rounding tolerance (4 dp of dollars on $5 = 0.02%) cannot explain 0.52pp.

## What was shipped

- `adversarial_pnl` accepts the `(mean, n)` tuple as well as a float and refuses
  a non-positive in-force gain rather than producing a sign-flipped bound.
- Two tests: the tuple seam, and the refusal. Both fail on the pre-change file.
- The `tie` fixture in `test_adversarial_pnl_charges…` was itself broken: the
  bars it used (close BELOW open) have no adverse-side bound under the module's
  own rule, and the assertion that the function returns `(x, 2, 2)` never
  described the module. Rebuilt in the shape the rule accepts.

## What was NOT changed, deliberately

- **No new estimate of the adverse fill.** The quantity the bound needs — the
  profit of a position **still open** at a bar — does not exist in
  `unified-paper.json`. `avg_gain_in_force` proxies it with *closed* trades
  selected by the stop they exited at, which is a conditioning on the outcome
  (a trade near its stop when it closes is usually closing *at* it: measured
  526 below-entry SL closers average **−2.29%**). With that input negative and
  mis-conditioned there is nothing to estimate, and inventing a positive
  constant would be the pre-declared-validation error this journal has recorded
  four times.
- **`summarise()` and the `agreement()` xfail stand as written.** The xfail's
  reason ("validates on only 1 of the book's recorded-fill records") is still the
  correct description of the state.
- **The 98.3% stamp is not called an edge or a loss.** It says the fill is
  one-sided; it does not say by how much, which is exactly the number the method
  cannot produce.

## The rule

> A test population that never crosses the seam is not coverage. Every fixture in
> this module passed a float to a function whose live caller builds it from a
> tuple, so the type contract of the bar method was asserted nowhere outside the
> one call site — and that call site was never executed against a real book
> either. Two defects stacked and the unexercised seam is what let the first one
> hide the second.

And the follow-on, which is the part worth carrying:

> A proxy for "edge in force" selected on the trade's exit is not a proxy, it is
> the outcome. When the conditioning variable is the thing being estimated, the
> number has the wrong sign by construction — and `abs()` makes it look fine.
