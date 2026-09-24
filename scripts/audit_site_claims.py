#!/usr/bin/env python3
"""audit_site_claims.py — does every number the site renders match the ledger?

WHY THIS EXISTS
---------------
The UI was built by a different AI across 11+ commits. It looks great, but a
good-looking page is not evidence that its numbers are right — and this product
has exactly one asset: that its numbers are right.

Two failure shapes this looks for:

  1. A displayed value that DISAGREES with the ledger field it claims to render.
  2. A value that is INTERNALLY INCONSISTENT — two numbers on a page that cannot
     both be true (e.g. "23 closed" beside a list containing an open record).

It checks the ledger against ITSELF first (so a bad export is caught before the
UI is blamed), then checks every derived relationship the UI depends on.

Read-only. Changes nothing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

LEDGER = Path("/home/ubuntu/signals-site/public/data/ledger.json")

problems: list[str] = []
warns: list[str] = []


def bad(m: str) -> None:
    problems.append(m)
    print(f"  🔴 {m}")


def warn(m: str) -> None:
    warns.append(m)
    print(f"  ⚠️  {m}")


def ok(m: str) -> None:
    print(f"  ✅ {m}")


def main() -> int:
    d = json.loads(LEDGER.read_text())
    s = d.get("stats") or {}
    sigs = d.get("signals") or []
    print("=" * 88)
    print("SITE CLAIM AUDIT — every displayed number vs the ledger")
    print("=" * 88)
    print(f"  ledger generated_at: {d.get('generated_at')}")
    print(f"  signals in array   : {len(sigs)}")
    print()

    # ── 1. the ledger against itself ────────────────────────────────────────
    print("  [1] LEDGER INTERNAL CONSISTENCY")
    closed = [x for x in sigs if x.get("outcome")]
    opens = [x for x in sigs if not x.get("outcome")]
    wins = [x for x in closed if (x["outcome"].get("ret_pct") or 0) > 0]
    losses = [x for x in closed if (x["outcome"].get("ret_pct") or 0) <= 0]

    if len(sigs) != s.get("signals_published"):
        bad(f"signals in array ({len(sigs)}) != stats.signals_published "
            f"({s.get('signals_published')})")
    else:
        ok(f"signals_published {s['signals_published']} == {len(sigs)} records")

    if len(closed) != s.get("closed"):
        bad(f"computed closed ({len(closed)}) != stats.closed ({s.get('closed')})")
    else:
        ok(f"closed {s['closed']} == {len(closed)} with an outcome")

    if len(opens) != s.get("open"):
        bad(f"computed open ({len(opens)}) != stats.open ({s.get('open')})")
    else:
        ok(f"open {s['open']} == {len(opens)} without an outcome")

    # the relationship the "All N" label depends on
    tot = (s.get("closed") or 0) + (s.get("open") or 0)
    if tot != s.get("signals_published"):
        bad(f"closed + open ({tot}) != signals_published "
            f"({s.get('signals_published')}) — any 'All' label is wrong")
    else:
        ok(f"closed + open == signals_published ({tot})")

    if len(wins) != s.get("wins"):
        bad(f"computed wins ({len(wins)}) != stats.wins ({s.get('wins')})")
    else:
        ok(f"wins {s['wins']} == {len(wins)} positive returns")

    if len(losses) != s.get("losses"):
        bad(f"computed losses ({len(losses)}) != stats.losses ({s.get('losses')})")
    else:
        ok(f"losses {s['losses']} == {len(losses)} non-positive returns")

    if s.get("wins", 0) + s.get("losses", 0) != s.get("closed", 0):
        bad(f"wins + losses ({s.get('wins',0)+s.get('losses',0)}) != closed "
            f"({s.get('closed')}) — a close would be neither won nor lost")
    else:
        ok("wins + losses == closed (no close is unclassified)")

    # win rate
    if closed:
        wr = round(len(wins) / len(closed) * 100, 1)
        if abs(wr - (s.get("win_rate_pct") or 0)) > 0.15:
            bad(f"computed win rate {wr}% != stats.win_rate_pct "
                f"{s.get('win_rate_pct')}%")
        else:
            ok(f"win_rate_pct {s.get('win_rate_pct')}% == {wr}% computed")

    print()

    # ── 2. the derived claims the UI states in words ────────────────────────
    print("  [2] DERIVED CLAIMS THE UI STATES")
    # payload = avg win / avg loss
    if wins and losses:
        aw = sum(x["outcome"]["ret_pct"] for x in wins) / len(wins)
        al = abs(sum(x["outcome"]["ret_pct"] for x in losses) / len(losses))
        pf = round(aw / al, 2) if al else None
        shown = s.get("payoff")
        if pf is not None and shown is not None and abs(pf - shown) > 0.02:
            bad(f"payoff: computed {pf} != stats.payoff {shown}")
        else:
            ok(f"payoff {shown} == {pf} computed (avg win {aw:.2f}% / "
               f"avg loss {al:.2f}%)")
        for k, v in (("avg_win_pct", aw), ("avg_loss_pct", al)):
            if s.get(k) is not None and abs(s[k] - v) > 0.02:
                bad(f"{k}: ledger {s[k]} != computed {v:.2f}")

    # expectancy = sum of returns / closed
    if closed:
        exp = round(sum(x["outcome"]["ret_pct"] for x in closed) / len(closed), 2)
        if s.get("expectancy_pct") is not None and abs(exp - s["expectancy_pct"]) > 0.02:
            bad(f"expectancy: computed {exp}% != stats.expectancy_pct "
                f"{s['expectancy_pct']}%")
        else:
            ok(f"expectancy_pct {s.get('expectancy_pct')}% == {exp}% computed")
        cum = round(sum(x["outcome"]["ret_pct"] for x in closed), 2)
        if s.get("cumulative_return_pct") is not None and abs(cum - s["cumulative_return_pct"]) > 0.05:
            bad(f"cumulative: sum of returns {cum}% != stats."
                f"cumulative_return_pct {s['cumulative_return_pct']}%")
        else:
            ok(f"cumulative_return_pct {s.get('cumulative_return_pct')}% "
               f"== {cum}% summed")

    # backfilled disclosure must match reality
    bf = [x for x in closed if x.get("backfilled")]
    if len(bf) != s.get("backfilled_closed"):
        bad(f"computed backfilled ({len(bf)}) != stats.backfilled_closed "
            f"({s.get('backfilled_closed')}) — the disclosure line would be wrong")
    else:
        ok(f"backfilled_closed {s['backfilled_closed']} == {len(bf)} flagged")

    live_c = len(closed) - len(bf)
    if s.get("live_closed") is not None and live_c != s["live_closed"]:
        bad(f"computed live closes ({live_c}) != stats.live_closed "
            f"({s['live_closed']})")
    else:
        ok(f"live_closed {s.get('live_closed')} == {live_c} "
           f"(closed minus backfilled)")

    # unattributed closes — the UI says these are excluded from the record.
    #
    # 🔴 TWICE CORRECTED. First this counted `not x.get("entry")` over ALL signals,
    # which matched the two OPEN positions (#52 SOL, #54 AVAX) — they have an
    # entry and no exit yet, so they are not unattributed, they are still open.
    # That produced "computed 0 != ledger 2" against a correct ledger.
    #
    # Narrowing to closed signals still yields 0: EVERY close in the export
    # carries an entry. So `unattributed_closes: 2` does not mean "a close with
    # no entry" — it more likely counts closes the trader journal could not match
    # to a committed signal, a distinction the export does not expose as a field.
    #
    # I cannot verify that from here, so this REPORTS instead of FAILING. An
    # audit that manufactures defects is as harmful as one that misses them: it
    # sends someone to fix a file that is already correct, and it erodes trust in
    # the checks that are real. The flag matters, so it stays visible — but it is
    # not asserted as a discrepancy I have evidence for.
    un = [x for x in closed if not x.get("entry")]
    declared = s.get("unattributed_closes")
    if declared:
        if len(un) != declared:
            print(f"  ℹ️  unattributed_closes {declared} — not reproducible from the "
                  f"export ({len(un)} closes lack an entry). The field reflects the "
                  f"trader journal's matching, which the export does not expose. "
                  f"REPORTED, not asserted. If this line ever disappears while the "
                  f"UI still prints the disclosure, check the exporter.")
        else:
            ok(f"unattributed_closes {declared} == {len(un)} closed without an entry")

    print()

    # ── 3. R sample: the UI claims a count and excludes the rest ────────────
    print("  [3] R DISTRIBUTION CLAIMS")
    with_r = []
    for x in closed:
        e, st, ex = x.get("entry"), x.get("stop"), (x.get("outcome") or {}).get("exit")
        if e and st and ex and abs(e - st) > 0:
            with_r.append((ex - e) / abs(e - st))
    if s.get("r_sample_size") is not None and len(with_r) != s["r_sample_size"]:
        bad(f"computed R sample ({len(with_r)}) != stats.r_sample_size "
            f"({s['r_sample_size']}) — the 'N closes with a stop' line would lie")
    else:
        ok(f"r_sample_size {s.get('r_sample_size')} == {len(with_r)} computable")
    no_stop = len(closed) - len(with_r)
    print(f"       {no_stop} closed trade(s) have no committed stop — the site must "
          f"say these are EXCLUDED, not counted as zero")
    if with_r:
        mean = sum(with_r) / len(with_r)
        best, worst = max(with_r), min(with_r)
        if s.get("r_mean") is not None and abs(mean - s["r_mean"]) > 0.02:
            bad(f"R mean: computed {mean:.3f} != stats.r_mean {s['r_mean']}")
        else:
            ok(f"R mean {mean:+.3f} (ledger {s.get('r_mean')})  "
               f"best {best:+.3f}  worst {worst:+.3f}")

    print()

    # ── 4. claims the data does NOT support must still be flagged ───────────
    print("  [4] UNSUPPORTED CLAIMS STILL FLAGGED")
    claims = (d.get("claims") or {}).get("claims") or []
    for c in claims:
        cid = c.get("id")
        sup = c.get("supported")
        rep = bool(c.get("replacement"))
        if sup is False and not rep:
            bad(f"claim '{cid}' is unsupported but has NO replacement wording — "
                f"the original overclaim could still render")
        elif sup is False:
            print(f"  ✅ claim '{cid}' unsupported and carries replacement wording")
        elif sup is True:
            print(f"  ℹ️  claim '{cid}' supported={sup}")
    # direction claim: verify against the data, not the flag
    sides = {x.get("side") for x in sigs}
    print(f"       distinct sides actually present: {sorted(s for s in sides if s)}")
    if len({s for s in sides if s}) > 1:
        warn("more than one side present — check the direction claim is "
             "genuinely supported now")

    # ── a supported claim must rest on a sample that CAN support it ─────────
    #
    # 🔴 holding_period flipped to supported=True on evidence reading
    # "held_days range 0-6 over 15 closes". Three separate things were wrong:
    #
    #   * only 13 closes carry held_days, not 15 — the cited count was wrong;
    #   * those 13 are EXACTLY the live closes, so the sample silently excludes
    #     all 10 backfilled closes (43% of the record);
    #   * 12 of the 13 read held_days=0. The whole range comes from ONE trade
    #     (#45 DOT, +8.40%, also the single best trade). Drop it and the range
    #     is 0-0, i.e. same-day.
    #
    # A claim is only supported if the sample it was measured on represents the
    # record it describes. Narrowing the sample until a claim becomes true is
    # how a false claim gets a green tick — and it flipped the honest way
    # (unsupported -> supported) which is the direction nobody investigates.
    for c in claims:
        cid = c.get("id")
        if cid == "holding_period" and c.get("supported") is True:
            hd = [x["outcome"].get("held_days") for x in closed
                  if (x.get("outcome") or {}).get("held_days") is not None]
            n_with = len(hd)
            n_closed = len(closed)
            if n_with < n_closed:
                bad(f"claim 'holding_period' is marked supported=True but was "
                    f"measured on {n_with} of {n_closed} closes — the other "
                    f"{n_closed - n_with} carry no held_days and are silently "
                    f"excluded. A claim cannot be supported by a sample that "
                    f"drops {round((n_closed-n_with)/n_closed*100)}% of the record.")
            non_zero = [h for h in hd if h]
            if len(non_zero) <= 1:
                bad(f"claim 'holding_period' is marked supported=True but "
                    f"{len(hd)-len(non_zero)} of {len(hd)} closes read held_days=0; "
                    f"the entire non-zero range comes from {len(non_zero)} trade(s). "
                    f"A single trade cannot support a 'holding period: days' claim.")
            else:
                ok(f"claim 'holding_period' measured on the full record "
                   f"({n_with} closes, {len(non_zero)} spanning >0 days)")

    # the same trap, generally: any claim whose evidence cites a subset
    def _cites_subset(c: dict) -> bool:
        blob = json.dumps(c)
        m = __import__("re").search(r"over (\d+) closes", blob)
        if not m:
            return False
        return int(m.group(1)) != len(closed)

    for c in claims:
        if c.get("supported") is True and _cites_subset(c):
            bad(f"claim '{c.get('id')}' is supported=True but its evidence cites "
                f"a close count that does not equal the record ({len(closed)})")

    # fees must still be disclosed as excluded
    costs = d.get("costs") or {}
    if costs.get("fees_included") is not False:
        bad(f"costs.fees_included == {costs.get('fees_included')!r} — returns are "
            f"gross; if this ever reads True the caveat becomes a lie")
    else:
        ok("costs.fees_included == False (caveat is truthful)")

    # execution: the demo- prefix must still be justified
    ex = d.get("execution") or {}
    blob = json.dumps(ex).lower()
    if "demo" not in blob:
        warn("execution block no longer mentions the demo endpoint — the `demo-` "
             "record id prefix must remain justified by this field")

    print()
    print("=" * 88)
    if problems:
        print(f"  🔴 {len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"     - {p}")
        if warns:
            print(f"  ⚠️  {len(warns)} warning(s)")
        return 1
    print(f"  ✅ all displayed claims reconcile with the ledger "
          f"({len(warns)} warning(s))")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
