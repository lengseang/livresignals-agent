# Short-rule certification verdict — 2026-10-24

**Status: NOT DEPLOYABLE. Zero of six rules certified. One hypothesis worth
forward-testing.**

Every number below is reproducible from the scripts named beside it. Where a
number is an in-sample optimum it says so. Where a sample is one fold it says so.

---

## 1. What was tested

The standing hunt (`reports/hunt5/findings.jsonl`, 65 findings) claimed six
net-positive short-breakdown rules:

| rule | hunt n | hunt net | hunt PF |
|---|---|---|---|
| bearcross 20/100 | 295 | +$38.83 | 1.93 |
| volbreak 55 ×3.0 | 237 | +$30.45 | 1.58 |
| volbreak 20 ×2.0 | 788 | +$49.88 | 1.48 |
| volbreak 100 ×1.5 | 648 | +$36.00 | 1.47 |
| volbreak 55 ×1.5 | 788 | +$42.15 | 1.40 |
| volbreak 34 ×1.5 | 735 | +$38.24 | 1.29 |

Those are **in-sample, best-variant-per-rule, over a rotating search space**, with
the hunt's own exit grid searched per rule. They are the number a backtest wants
you to look at.

## 2. Fee stress — they survive, and that is real

`scripts/certify_short_rules.py` rescales each result to a higher round-trip cost
(arithmetic on the stored run, not a fresh backtest):

| rule | net @0.20% RT | @0.30% | @0.40% | @0.50% |
|---|---|---|---|---|
| bearcross 20/100 | +38.83 | +37.36 | +35.88 | +34.41 |
| volbreak 20 ×2.0 | +49.88 | +45.94 | +42.00 | +38.06 |
| volbreak 100 ×1.5 | +36.00 | +32.76 | +29.52 | +26.28 |

**All six remain positive at 0.50% round trip — 2.5× the hunt's assumption.** Fees
are not what kills these rules. Contrast the live dip-buyer, whose entire edge at
its best hold was **+0.125%** against a **0.15–0.20%** spot cost.

## 3. Walk-forward — none of them certify

`scripts/certify_walkforward.py`, 4 consecutive folds over 2023-03-23 → 2026-10-24,
exit fixed in advance (stop 4% / target 12% / hold 120 bars), 0.30% RT:

| rule | n | net | positive folds | per-fold net |
|---|---|---|---|---|
| volbreak 20 ×2.0 | 1138 | **+17.72** | 3/4 | +15.0, **−10.8**, +6.1, +7.5 |
| volbreak 100 ×1.5 | 838 | +16.51 | 3/4 | +12.2, **−2.5**, +4.6, +2.2 |
| volbreak 55 ×1.5 | 1072 | +14.07 | 3/4 | +18.6, **−8.3**, +2.9, +0.8 |
| volbreak 55 ×3.0 | 524 | +8.77 | 2/4 | +4.4, **−2.3**, −0.2, +6.9 |
| volbreak 34 ×1.5 | 1284 | +7.26 | 2/4 | +18.3, **−12.4**, +1.7, −0.4 |
| bearcross 20/100 | 568 | **−1.33** | 2/4 | +8.3, **−7.8**, −2.4, +0.6 |

**Certifiable: 0 of 6.**

Two things stand out:

1. **Every rule loses in fold 2.** Six independent mechanisms failing in the same
   period is not six coincidences.
2. **The aggregate collapses versus the hunt** — volbreak 20×2.0 from +$49.88 to
   +$17.72; bearcross 20/100 from +$38.83 to −$1.33. Fixed exits and honest folds
   remove the in-sample selection.

## 4. Why fold 2 — the rules are a regime bet, not a strategy

BTC over the same folds:

| fold | period | BTC | rules |
|---|---|---|---|
| 1 | 2023-03 → 2024-02 | **+73.3%** UP | 5/6 positive |
| 2 | 2024-02 → 2025-01 | **+104.3%** UP | **0/6 positive** |
| 3 | 2025-01 → 2025-11 | −10.9% DOWN | 6/6 positive |
| 4 | 2025-11 → 2026-10 | −11.4% DOWN | 5/6 positive |

The rules are short-only. They make money when the market falls and lose when it
rises. Fold 2 was the largest rally in the sample (+104%), and it wiped out every
rule. **This is not a defect to tune away — it is what a short rule is.**

## 5. The one hypothesis worth forward-testing

Gate the shorts to when BTC is below its 200-bar (4h) SMA:

| fold | BTC vs 200-SMA at fold start | gate | rule net |
|---|---|---|---|
| 1 | +19.2% | shorts OFF | +15.0 (missed) |
| 2 | +14.4% | shorts OFF | **−10.8 (avoided)** |
| 3 | +3.5% | shorts OFF | +6.1 (missed) |
| 4 | −7.4% | **SHORTS ON** | **+7.5 (caught)** |

The gate is ON in exactly one fold and that fold is positive; the fold that lost
−$10.80 is gated off.

**That is n=1 observed regime. It is a hypothesis with one confirming observation,
NOT a result.** It is recorded here because it is cheap to forward-test and it is
the only configuration in this whole exercise where the losing period and the
profitable period separate cleanly.

## 6. What was NOT deployed, and why

- The six short rules — 0/6 certify.
- A 200-SMA regime gate — n=1 fold.
- Moving the dip-buyer to futures — the edge (+0.125% at its best in-sample hold)
  is smaller than the futures fee; and the repo's own out-of-sample tests on
  ~3,000 bars already measured **−0.364%** for that entry and paused it.
- Reverting the hold from 2.0h to 6.0h — the calibrator's own candidate table has
  2.0h as the optimum (+0.125%) and 6.0h as second-worst (−0.438%). Reverting
  would have made the live path worse.

No live config was changed. `data/exit-params.json` is unmodified (`hold_hours`
still 2.0).

## 7. Next

Forward-test the regime gate as a paper lane: shorts enabled only when BTC is
below its 200-bar SMA, same fixed exit, recording every fold-length period. A
single down-regime is not enough evidence to risk money, and a paper lane costs
nothing to run while it accumulates.
