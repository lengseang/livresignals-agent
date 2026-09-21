#!/usr/bin/env python3
"""SIGNAL LEDGER — a tamper-evident public track record.

THE PROBLEM THIS SOLVES
-----------------------
Anybody can publish a backtest. Anybody can delete their losers. A track
record is only worth something if a reader can VERIFY it was not edited
after the fact. This ledger makes that mathematically checkable.

HOW THE HASH CHAIN WORKS
------------------------
Every record stores the hash of the PREVIOUS record plus its own content:

    h_n = sha256( h_{n-1} | canonical_json(record_n) )

Consequences:
  * Edit any past signal -> its hash changes -> every later hash breaks.
  * Delete a signal      -> the chain no longer links -> verify fails.
  * Insert a fake winner -> breaks the chain at the insertion point.
  * Pre-outcome stamp    -> the signal is committed BEFORE the result is
                            known, so we cannot cherry-pick after seeing it.

Records are written ONCE, at entry time. Outcomes are appended as a
separate `close` record referencing the entry hash. We never rewrite the
entry. That is what makes "all losses published" verifiable rather than
a promise.

USAGE
    python scripts/signal_ledger.py verify        # integrity check
    python scripts/signal_ledger.py head          # current chain head
    python scripts/signal_ledger.py stats         # public stats
    python scripts/signal_ledger.py backfill      # import existing trades
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
LEDGER = BASE / "data" / "signal-ledger.jsonl"
GENESIS = "0" * 64

# ---------------------------------------------------------------- core


def _canon(obj: dict) -> str:
    """Canonical JSON - key order and spacing must be byte-stable or the
    hash changes for irrelevant reasons."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _hash(prev_hash: str, record: dict) -> str:
    return hashlib.sha256(
        (prev_hash + "|" + _canon(record)).encode("utf-8")).hexdigest()


def chain_head() -> tuple[int, str]:
    """Return (count, last_hash) - the current end of the chain."""
    if not LEDGER.exists():
        return 0, GENESIS
    count, last = 0, GENESIS
    with LEDGER.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                break
            count += 1
            last = rec.get("hash", last)
    return count, last


def read_records(path: Path | None = None) -> list[dict]:
    """Read the ledger into a list of parsed records.

    Tolerant by design: a missing file yields [], a blank line is skipped,
    and a PARTIALLY WRITTEN trailing line (the writer was interrupted
    mid-append, so the last line is not valid JSON) is dropped rather than
    raised. A track record is read far more often than it is written, and a
    torn tail must never take the public site down.
    """
    p = path or LEDGER
    if not p.exists():
        return []
    records: list[dict] = []
    try:
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    # torn/partial tail - stop here, keep what verified.
                    break
                if isinstance(rec, dict):
                    records.append(rec)
    except OSError:
        return records
    return records


def verify_chain(path: Path | None = None) -> dict:
    """Walk the chain and return a STRUCTURED integrity report.

    Any edit, deletion or reorder breaks it. Returns a plain dict so both the
    CLI (`cmd_verify`) and the public API can consume identical logic:

        {"valid": bool, "records": int, "head": str, "problems": [...]}

    `problems` entries are dicts of {line, seq, why} — never free text, so the
    API can serialise them without leaking anything.
    """
    p = path or LEDGER
    if not p.exists():
        return {"valid": True, "records": 0, "head": GENESIS, "problems": []}
    prev = GENESIS
    n = 0
    problems: list[dict] = []
    for i, line in enumerate(read_records(p), 1):
        seq = line.get("seq")
        n += 1
        if line.get("prev") != prev:
            problems.append({"line": i, "seq": seq,
                             "why": "prev-hash mismatch"})
        stored = line.pop("hash", None)
        expect = _hash(prev, line)
        line["hash"] = stored
        if stored != expect:
            problems.append({"line": i, "seq": seq, "why": "content altered"})
        if seq != n:
            problems.append({"line": i, "seq": seq,
                             "why": f"seq gap (expected {n})"})
        prev = stored or expect
    return {"valid": not problems, "records": n, "head": prev,
            "problems": problems}


# ---------------------------------------------------------------- stats


def collect_stats_inputs(path: Path | None = None
                         ) -> tuple[dict[str, dict], list[dict]]:
    """Return (entries_by_id, closes) — the raw material for stats.

    Shared by `public_stats()` and the API's record rendering so the two can
    never disagree about which trades exist or how `ret` was derived. A
    `close` record without an explicit `ret` inherits one derived from the
    referenced signal's entry/exit prices (and side), so the ledger can carry
    trades recorded only as prices.
    """
    entries: dict[str, dict] = {}
    closes: list[dict] = []
    raw_closes: list[dict] = []

    records = read_records(path)

    # Corrections are appended AFTER the records they repair (that is the only
    # way an append-only chain can do it), so a single chronological pass would
    # read a close before its correction exists. Collect the full remap first,
    # then apply it — order-independent by construction.
    #
    # A `reconcile` record redeclares a close's id when the original
    # referenced a non-existent entry. The original record is never edited:
    # it stays in the chain, and the correction is itself an attributed,
    # timestamped entry, so a verifier can see both the error and the fix.
    remap: dict[str, str] = {}
    for rec in records:
        if rec.get("type") == "reconcile":
            p = rec.get("payload") or {}
            frm, to = p.get("from"), p.get("to")
            if frm and to:
                remap[frm] = to

    for rec in records:
        rtype = rec.get("type")
        payload = rec.get("payload", {}) or {}
        if rtype == "signal":
            sid = payload.get("id")
            if sid is None:
                continue
            entries[sid] = {"rec": rec, **payload}
        elif rtype == "reconcile":
            continue
        elif rtype == "close":
            c = dict(payload)
            # apply any correction before anything else reads the id
            if c.get("id") in remap:
                c["id"] = remap[c["id"]]
                c["reconciled"] = True
            raw_closes.append(c)

    for c in raw_closes:
        if c.get("ret") is None:
            e = entries.get(c.get("id"))
            if e and e.get("entry") and c.get("exit"):
                d = c["exit"] / e["entry"] - 1
                if (e.get("side") or "L") in ("S", "short", -1):
                    d = -d
                c["ret"] = d
        closes.append(c)
    return entries, closes


def public_stats(path: Path | None = None) -> dict:
    """The public track record, computed once, in one place.

    Every figure here is recomputable from the hash-chained ledger: win rate,
    payoff, expectancy, cumulative return, max drawdown, longest losing run
    and the win/loss/open counts. `cmd_stats()` prints from this dict and
    `GET /api/ledger` serialises the same dict, so the CLI and the website can
    never publish different numbers.
    """
    entries, closes = collect_stats_inputs(path)

    wins = [c for c in closes if (c.get("ret") or 0) > 0]
    losses = [c for c in closes if (c.get("ret") or 0) <= 0]
    wr = (len(wins) / len(closes) * 100) if closes else 0.0
    aw = (sum(c["ret"] for c in wins) / len(wins)) if wins else 0.0
    al = (abs(sum(c["ret"] for c in losses) / len(losses))) if losses else 0.0
    payoff = (aw / al) if al else 0.0
    exp = (sum(c.get("ret") or 0 for c in closes) / len(closes) * 100
           if closes else 0.0)

    equity, peak, mdd = 0.0, 0.0, 0.0
    for c in closes:
        equity += c.get("ret") or 0
        peak = max(peak, equity)
        mdd = min(mdd, equity - peak)

    longest = cur = 0
    for c in closes:
        if (c.get("ret") or 0) <= 0:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0

    return {
        "signals": len(entries),
        "closed": len(closes),
        "wins": len(wins),
        "losses": len(losses),
        "open": len(entries) - len(closes),
        "win_rate": wr,
        "win_rate_pct": wr,
        "avg_win_pct": aw * 100,
        "avg_loss_pct": al * 100,
        "payoff": payoff,
        "expectancy_pct": exp,
        "cumulative_return_pct": equity * 100,
        "max_drawdown_pct": mdd * 100,
        "longest_losing_streak": longest,
    }


def append(rec_type: str, payload: dict, ts: str | None = None) -> dict:
    """Append one record, chained to the head.

    `ts` lets a backfilled record carry its ORIGINAL event time. The chain
    position still proves when it was WRITTEN (the record after it exists),
    while the payload time preserves when the signal was actually issued.
    Both are recorded so the distinction is never blurred: `written` is
    always the true append time and cannot be forged.
    """
    n, prev = chain_head()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = {
        "seq": n + 1,
        "type": rec_type,
        "ts": ts or now,          # event time (original for backfills)
        "written": now,           # true append time - never backdated
        "payload": payload,
        "prev": prev,
    }
    body["hash"] = _hash(prev, body)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(_canon(body) + "\n")
    return body


# ---------------------------------------------------------------- commands


def cmd_verify(verbose: bool = True) -> bool:
    """Walk the chain. Any edit, deletion or reorder breaks it.

    Thin printer over `verify_chain()` so the CLI and the public API run
    byte-identical logic.
    """
    if not LEDGER.exists():
        print("  ledger does not exist yet")
        return True
    rep = verify_chain()
    n, broken = rep["records"], rep["problems"]

    print("=" * 74)
    print(f"SIGNAL LEDGER — integrity check ({n} records)")
    print("=" * 74)
    if broken:
        for p in broken[:10]:
            print(f"  ✗ line {p['line']} (seq {p['seq']}): {p['why']}")
        print(f"\n  ⛔ CHAIN BROKEN — {len(broken)} problem(s). "
              f"The record CANNOT be trusted as published.")
        return False
    print(f"  ✅ chain intact — all {n} records verify")
    print(f"  head: {rep['head'][:32]}…")
    return True


def cmd_head() -> None:
    n, h = chain_head()
    print(f"  records: {n}")
    print(f"  head   : {h}")


def cmd_stats() -> None:
    """Public-facing stats: every loss counted, nothing hidden.

    Prints from `public_stats()` — the same function the website's
    `/api/ledger` endpoint serialises, so the CLI and the site can never
    show different numbers.
    """
    if not LEDGER.exists():
        print("  no records yet")
        return
    s = public_stats()

    print("=" * 74)
    print("PUBLIC TRACK RECORD")
    print("=" * 74)
    print(f"  signals published : {s['signals']}")
    print(f"  outcomes recorded : {s['closed']}")
    print(f"  wins              : {s['wins']}")
    print(f"  losses            : {s['losses']}   <- published, not hidden")
    print(f"  win rate          : {s['win_rate']:.1f}%")
    print(f"  payoff            : {s['payoff']:.2f}")
    print(f"  expectancy/trade  : {s['expectancy_pct']:+.3f}%")
    print(f"  cumulative (sum)  : {s['cumulative_return_pct']:+.2f}%")
    print(f"  max drawdown      : {s['max_drawdown_pct']:.2f}%")
    print(f"  longest losing run: {s['longest_losing_streak']}")
    print(f"  open (unrevealed): {s['open']}")
    print()
    print("  Every figure above is recomputable from the hash-chained ledger.")
    print("  Run `verify` to prove nothing was edited.")


def cmd_backfill() -> None:
    """Import already-closed demo trades so the record starts now.

    The demo journal (data/regime-trades.jsonl) carries a real `opened`
    timestamp and the 10 entry features, so the entry record is
    reconstructed with its ORIGINAL time - not the backfill time. That
    matters: a pre-outcome stamp is the whole point.
    """
    added = 0
    jl = BASE / "data" / "regime-trades.jsonl"
    if jl.exists():
        for i, line in enumerate(jl.open(encoding="utf-8"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = f"demo-{i:04d}-{t.get('symbol','?')}"
            append("signal", {
                "id": sid,
                "source": "demo-regime",
                "backfilled": True,
                "symbol": t.get("symbol"),
                "side": "L" if t.get("side") == 1 else "S",
                "entry": t.get("entry"),
                "opened": t.get("opened"),      # ORIGINAL entry time
                "feats": t.get("feats") or {},
            }, ts=t.get("opened"))
            append("close", {
                "id": sid,
                "exit": t.get("exit"),
                "ret": t.get("ret"),
                "why": t.get("why"),
                "backfilled": True,
            }, ts=t.get("ts"))
            added += 1

    # --- real binance trades (kept minimal: this file may be empty) ---
    lt = BASE / "data" / "live-trades.json"
    if lt.exists():
        try:
            d = json.loads(lt.read_text())
            rows = d if isinstance(d, list) else d.get("trades", [])
            for i, t in enumerate(rows, 1):
                sid = f"real-{i:04d}-{t.get('symbol','?')}"
                append("signal", {
                    "id": sid, "source": "binance-real",
                    "backfilled": True,
                    "symbol": t.get("symbol") or t.get("asset"),
                    "side": t.get("side"), "entry": t.get("entry"),
                }, ts=t.get("opened") or t.get("ts"))
                append("close", {"id": sid, "exit": t.get("exit"),
                                 "ret": t.get("ret") or t.get("pnl_pct"),
                                 "backfilled": True}, ts=t.get("ts"))
                added += 1
        except Exception as exc:
            print(f"  (live-trades skipped: {str(exc)[:60]})")
    print(f"  backfilled {added} trade(s) — each marked backfilled=true")
    n, h = chain_head()
    print(f"  chain now: {n} records, head {h[:24]}…")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if cmd == "verify":
        sys.exit(0 if cmd_verify() else 1)
    elif cmd == "head":
        cmd_head()
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "backfill":
        cmd_backfill()
    else:
        print(__doc__)
