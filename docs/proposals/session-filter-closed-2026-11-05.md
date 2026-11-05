# The session filter was never tested, and it was never retired

Overnight shift, 2026-11-05. The improvement loop has been emitting this line
on every run since the first commit:

```
priority 4  explore
"Explore: time-of-day filter — capitulation bounces may cluster around US
 session open (13:30-16:00 UTC). Test if a session filter raises win rate."
```

It is a **hardcoded string literal** in `_suggest()` (`scripts/improvement_loop.py`).
Nothing reads the ledger to retire it, and nothing special-cases it when it is
answered. The volume-gate idea next to it was moved to `explore_result` with a
`CLOSED (tested NEGATIVE …)` prefix when it was run — so the codebase already
has the convention; this one simply never went through it.

That matters beyond one line of text. A suggestion channel whose items cannot
be closed is not a to-do list, it is a **fixed cost paid on every run**: the
owner reads the same un-actioned idea every 40 minutes, and `priority 4` sits
under real findings, so it is easy to skim past — which is exactly what
happened here for the whole life of the repo.

## It is now tested, and it is negative

Design, fixed **before** any outcome was inspected (`research/_session_cluster_probe.py`):

- Book: `data/unified-paper.json` `closed`, through the production
  `paper_book.collapse_multitf` (SYSTEM_MAP #6) — 3573 raw → 2798 collapsed,
  775 removed.
- Unit: **net** pnl (`pnl_net`), costed records only. The 1986 uncosted records
  are dropped, not guessed; every one of them is pre-2026-10-10 (before the
  measured-cost change) and carries `loose: true`.
- Key: UTC hour of `ts_utc`, four a-priori bands — Asia 00-06, London 07-12,
  NY 13-20, Late 21-23. No post-hoc regrouping is reported.

Costed book: **n=812, net −3.48, mean −0.00429/trade**, 2026-10-06..11-05.

```
band        n      net     mean   t vs rest
Asia      221   -3.161  -0.01430     -2.06
London    190   +0.039  +0.00020     +0.99
NY        328   +0.524  +0.00160     +1.64
Late       73   -0.886  -0.01214     -0.88
```

Asia looks real at t=−2.06. Two checks kill it.

**1. Every band flips sign between halves.** Split at 2026-10-22:

```
H1: Asia m=-0.0253 t=-3.24 | NY m=+0.0102 t=+2.40 | London m=-0.0041
H2: Asia m=-0.0076 t=+0.15 | NY m=-0.0109 t=-0.42 | London m=+0.0043
```

Asia goes from the strongest negative to flat; NY goes from the strongest
positive to flat. A band that inverts is the period, not the session.

**2. NY's entire positive is one day.** 2026-10-18 contributes **+3.21** of
NY's **+0.52** total — the breadth flush already documented in
`docs/proposals/unified-book-one-day-dependency-2026-10-23.md`. Remove it:

```
without 10-18: n=779 net=-6.670
  Asia   n=218 net=-3.122 t=-1.21
  London n=189 net=+0.024 t=+1.96
  NY     n=299 net=-2.686 t=-0.12
  Late   n= 73 net=-0.886 t=-0.40
```

NY drops from t=+1.64 to t=−0.12. The most flattering band in the first pass is
negative once one day is removed. London's t rises to +1.96 as a *side effect*
of removing that day, and it does not survive either: its hourly cells run
+0.201, −0.497, −0.086, +0.834, −0.329, −0.099, and its own halves are
−0.00485 then +0.00505. An artefact that depends on which day you drop is not a
session effect.

**Verdict: no session effect is measurable on this book at n=812.** Not "shorts
in Asia" — the sample cannot see it.

## What this does not claim

- Not that sessions *cannot* matter. It says this book cannot show it: 29
  distinct days, one day worth 3.19 of a −3.48 total, and four bands cut from
  the same 812 trades are not independent tests (a family of four is not
  corrected for anywhere above).
- No live path, parameter, disable list, or book was touched. The probe is a
  read-only script; `scripts/improvement_loop.py` changes only the *text* of a
  suggestion and how it is classified.

## Shipped

`scripts/improvement_loop.py`: the `explore` item becomes an `explore_result`
with a `CLOSED (tested NEGATIVE 2026-11-05)` prefix carrying the numbers above —
same shape as the volume-gate entry beside it. `tests/test_improvement_explore_closure.py`
asserts (a) no `explore`-type suggestion is emitted for a hypothesis that
already has a recorded closed result, and (b) the closed result carries the
evidence reference that makes it checkable. Both fail on the previous code.
