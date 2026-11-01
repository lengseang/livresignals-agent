# The blackout window gated *every* high-impact US release, and the fallback was an archive

Date: 2026-11-01 · File: `scripts/econ_calendar.py` · New test file:
`tests/test_econ_calendar_blackout.py` (10 tests, 5 of them red before the fix).

Found while re-deriving the blackout state by hand before writing the morning
report, not from a failing test or a red monitor. All three defects are in the
event-calendar reader that the demo crypto book consults before every entry
(`bybit_demo_trader.py:166`).

## Defect 1 — the trigger was impact+country, not "an event that moves this book"

`risk_state()` opened a T-24h/+6h blackout on any `High`-impact `USD` release.
Its only caller is a **crypto** book: `SYMS = BTC, ETH, SOL, XRP, ADA, LINK,
DOGE, AVAX, NEAR, DOT, ATOM, LTC, XAUT`. The module already carries the list of
releases that actually move that book — `GOLD_MOVERS`, the same set
`gold_macro` builds its gate on — and `is_gold_mover()` already exists. It was
simply never consulted by the blackout.

Measured on today's calendar: `2026-11-03 10:16 USD Congressional Elections` is
`High` impact and is **not** in `GOLD_MOVERS`. Under the old rule it opened a
30-hour stand-down for the whole book. A blanket window is not free: it is 30
hours of missing every entry the strategy would have taken, for a reason the
book does not act on.

Now: `High` + `USD/US` + `is_gold_mover(title)`. NFP, CPI, PCE, FOMC, Powell and
the interest-rate decisions all still gate (test:
`test_high_usd_gold_mover_still_blackouts`). Low-impact and non-USD gold movers
still do not. The `next` pointer still names *any* upcoming High USD release,
so the card keeps telling the owner what is coming even when it is not gated
(`test_next_pointer_still_names_a_gated_out_high_release`).

## Defect 2 — the same release, delivered twice, read as two releases

`FEEDS` is `thisweek` + `nextweek`. They overlap on the boundary days, and
`fetch_events()` extended one list with the other and wrote both. The live
cache held **116 rows / 113 unique** on 2026-11-01 (`Trade Balance` 11-04 08:30,
`Unemployment Rate` 11-06 08:30, `Daylight Saving Time Shift` 11-01 02:00, each
twice). Nothing downwind double-fires today because `risk_state` returns the
first match — but "the list contains each release exactly once" was an
assumption nothing enforced, and the two feeds can stamp one release with
different UTC offsets (this-week is `-05:00`, next-week `-04:00` across the DST
boundary that fell today).

Collapsed before the write, keyed on ISO minute + country + title — minute
rather than instant, because the offset can differ while the clock does not.
Same minute with two titles (NFP + Unemployment Rate), or two countries, stays
two events. After the fix a live fetch returned **119 events, 0 duplicates**.

## Defect 3 — the fallback seed was an archive, and it can overwrite a healthy cache

`_save_seed()` merged forever; `_load_seed()` returned everything. On
2026-11-01 the feed was bot-challenged (HTTP 429 on `curl`), `fetch_events`
fell back to the seed, and the cache went **116 rows → 874 rows spanning seven
weeks, 762 of them already past**. That is the mechanism by which a *healthy*
116-event cache is replaced by a month of history the moment the network
hiccups once.

Pruned to `[-2d, +21d]` on merge **and** on fallback read. Old past rows can be
neither upcoming nor inside a blackout, so dropping them changes no decision;
a seed that only grows is not a fallback. Test:
`test_blocked_fetch_falls_back_to_a_pruned_seed`.

## Not changed

The live cache was restored to its pre-investigation contents
(`git status` clean for `data/`). `data/ff-seed.json` still holds the 874 rows
— it prunes on its next successful merge, and rewriting it by hand would be a
write to a prod data file that no test covers.

## Left open, reported to the owner

`state/crypto-trend-demo/state.json` — `ETH_TURTLE_H4` holds `in_pos: true` on
`ETHUSDT` with `ledger_id: null`, while `RANDOM_ENTRY_CONTROL` (its paired
arm) owns the same symbol with a committed ledger id. This is the double claim
the 2026-10-31 pass released; it re-appeared at 00:07 UTC today. Held and not
repaired from the autonomous seat (a live demo/exchange state file, no failing
test, and the repair is the owner's call). Escalated as E9.

Lesson for the map: **a safety gate must name the population it protects.**
Rule 3 asks the live book how to tune exits (#15); rule 14 asks whether an edge
can still trade. This one asked "is the US calendar busy?" of a book that
trades crypto, and answered by standing it down for 30 hours.
