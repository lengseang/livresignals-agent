# V2-18 results: rc4, scaling from 3 to 15 symbols

* Branch `v2/scale-rc4`; release branch `v2/release-rc4`. Stacked on rc3.
* Paper only. Nothing here can place a real order.

## 1. The problem, found by measuring rather than guessing

The rc3 runner handled symbols one after another at about 2 s each (measured in the smoke test). With 15 symbols that is
about 30 s from the bar closing to the last decision, against a 20 s staleness ceiling, so roughly the last third of the
coins would have been silently skipped as `snapshot_unusable`.

## 2. What rc4 changes

| change | why |
|---|---|
| network phases run concurrently: priming, hourly context refresh, daily stances, every symbol's bars | these were all sequential fetch loops |
| order books of the coins whose decision is due are fetched together, right before the decisions (reusable for up to 4 s; the feature engine itself rejects books older than 5 s) | a refetch per coin cost about 1 s each |
| decisions, Jev, the paper book and the chains stay **sequential in the configured order** | determinism: two runs give identical records (only the real-clock `latency_ms` differs), and the paper chain is byte-identical |
| `JevBudget`: a daily cap on paid Jev calls per lane, shared by the entry and position clients (approved: 4 000/day) | scaling multiplies the bill; over the cap the gateway answers, labelled `balance_exhausted` |
| presets `top3`, `top8`, `all15` for `--symbols` / `V2_SYMBOLS` | the rollout steps |
| heartbeat: `timing`, `by_symbol`, `book_prefetch`, `jev_budget`; scorecard `by_symbol`; watchdog `jev_budget` and `stale_snapshots` alerts | so each rollout gate is a number you can read |
| preflight `jev_key_authenticates` (one ~100-token call) | see section 4 |

The 20 s staleness ceiling is unchanged. It is a safety limit.

## 3. Live measurement: 15 symbols, real Binance data, 7 decision boundaries each

(The runs used a 1-minute cadence to get 7 boundaries in 7 minutes. This sandbox's Binance round trip is about 1 s, so it is
pessimistic compared with a VPS.)

| | first live run: concurrent bar fetch only | rc4 (final) |
|---|---|---|
| decisions skipped as stale | 15 of 105 (ATOM 7/7, LTC 6/7, DOT 2/7) | **0 of 105** |
| slowest step | 20.3 s | 10.6 s |
| oldest snapshot used | 22.5 s (over the 20 s ceiling) | 12.6 s |
| order-book prefetch | none | 80 hits, 25 stale re-fetches |
| feed failures | 0 | 0 |

The left column is a build with concurrent bar fetching but NOT the separate order-book prefetch phase. It shows that
concurrent bar fetching alone was not enough: the order book was the other half of the cost. rc3 itself (fully sequential) was
never run live at 15 symbols; its ~30 s is an extrapolation from the 3-symbol smoke test (about 2 s per symbol), which is why
the problem was caught before the rollout rather than during it.

## 4. What the live runs also showed

* **The development Jev key no longer authenticates** (HTTP 403, `authentication_error`). That is consistent with it having
  been rotated, so it is expected. The system degraded exactly as designed: 195 of 195 decisions across both runs came from
  the labelled gateway, none crashed, and every call was counted against the budget.
* **But a rotated, revoked or mistyped key passed preflight**, because preflight only checked that a key was present. The
  whole service would have run on the gateway with nothing blocking the start (the watchdog would alert after 10 calls).
  Preflight now makes one minimal call, so a bad key blocks the start. The check never prints the key and sends it only to
  `api.typesafe.ai`.
* **The real Jev's latency at 15 symbols was not measured in these runs** (no valid key). The failing 403 call takes about as
  long as a real one, so the timing above is a fair proxy, but the rollout gates read the real numbers from the VPS.

## 5. Audit notes

* A first version of the watchdog's stale rule averaged over all coins, so one dead coin among 15 (6.7% overall) would never
  have alerted. It now also alerts when any single coin is skipped more than half the time (after 8 boundaries).
* A mutation check showed the tests did not pin the symbol order of decisions; that test was added.
* 5 mutations on the new logic (sequential fetching, a budget that never refuses, a stale-book reuse, swallowed thread errors,
  reversed symbol order): all caught, after the order test was added.

**Tests:** 1,293 passed, 3 skipped, 0 failed for `tests/loop`.

## 6. Rollout

`docs/v2/SCALE-ROLLOUT-PROMPT.md` is the prompt for the VPS agent: upgrade to rc4 on the same 3 coins, prove it, widen to
`top8`, hold 2 days, then `all15` with the owner's go-ahead. The gates are in `docs/v2/RUNBOOK.md`.
