#!/usr/bin/env python3
"""EXPORT THE LEDGER AS STATIC JSON FOR THE WEBSITE.

WHY THIS EXISTS INSTEAD OF A PUBLIC API
---------------------------------------
The original design had the Next.js site fetch the ledger from a public API
on the VPS (hermes.lengseang.dev). That requires opening ports 80/443 to the
internet, which would also expose the *dashboard* routes on the same origin
(/api/live-balance, /api/trades, /api/positions) — the live trading book.

This exporter removes the exposure entirely:

    VPS writes ledger  ->  ledger.json committed to the site repo
                       ->  Vercel serves it as a static asset
                       ->  the site reads its own /data/ledger.json

Result: no inbound ports, no public endpoint, no shared origin with the
private dashboard. The site becomes fully static and the cost is freshness —
which for a record whose unit is *days* is not a cost at all.

It also means the git history of the site repo is a second, independent
audit trail of the record.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
LEDGER = BASE / "data" / "signal-ledger.jsonl"
SITE = Path("/home/ubuntu/signals-site")
OUT = SITE / "public" / "data" / "ledger.json"

sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "src"))


def build_payload() -> dict:
    """Produce the same shape the API served, so the frontend needs no change.

    Reuses signal_ledger's own readers so this can never disagree with the
    CLI verifier or the stats command — and, for every PUBLISHED figure, uses
    record_model, which derives all of them from one closed-set definition.
    """
    import signal_ledger as sl
    import record_model as rm

    records = sl.read_records()
    entries_by_id, closes_list = sl.collect_stats_inputs()
    raw_stats = sl.public_stats()
    model = rm.build_model()

    # `closes_list` is ALREADY flattened by signal_ledger into
    # {id, ret, exit, why, backfilled}. Index it by id and reuse those exact
    # values, so this export can never disagree with the CLI verifier.
    closes: dict[str, dict] = {}
    for c in closes_list:
        ref = c.get("id")
        if ref:
            closes[ref] = c

    signals = []
    for sig_id, w in entries_by_id.items():
        rec = w.get("rec", {})
        p = dict(rec.get("payload", {}))
        close = closes.get(sig_id)
        outcome = None
        if close:
            ret = close.get("ret")
            outcome = {
                "exit": close.get("exit"),
                "ret": ret,
                "ret_pct": (ret * 100) if isinstance(ret, (int, float)) else None,
                "pnl": close.get("pnl"),
                "why": close.get("why"),
                "held_days": close.get("held_days"),
                "closed_ts": close.get("closed_ts") or close.get("ts"),
                "backfilled": bool(close.get("backfilled")),
            }
        signals.append({
            "id": sig_id,
            "seq": rec.get("seq"),
            "ts": rec.get("ts"),
            "written": rec.get("written"),
            "hash": rec.get("hash"),
            # The link to the previous record. This is what makes a chain
            # legible: without it a reader sees a hash and has to take the
            # "chained" claim on faith. With it, the detail page can show
            # which record this one commits to, and the verifier can be
            # walked by hand.
            "prev": rec.get("prev"),
            "symbol": w.get("symbol") or p.get("symbol"),
            "side": w.get("side") or p.get("side"),
            "source": w.get("source") or p.get("source"),
            "entry": w.get("entry") or p.get("entry"),
            "qty": p.get("qty"),
            "stop": p.get("stop"),
            "tp_r": p.get("tp_r"),
            "risk_pct": p.get("risk_pct"),
            "stop_atr": p.get("stop_atr"),
            "exit_mode": p.get("exit_mode"),
            "regime": p.get("regime"),
            "gate": p.get("gate"),
            "feats": w.get("feats") or p.get("feats"),
            "backfilled": bool(w.get("backfilled") or p.get("backfilled")),
            "outcome": outcome,
        })

    # stable order by ledger sequence
    signals.sort(key=lambda s: (s.get("seq") or 0))

    # Closes with no committed entry. These are REAL trades whose entry
    # predates the ledger, so we know they closed but have no committed entry
    # to verify against. Published explicitly rather than silently dropped —
    # dropping them would hide trades, counting them would break the totals.
    #
    # Note: `closes` was populated from closes_list and then DRAINED by the
    # signal join above (closes.get(sig_id) with no removal — so build the set
    # of ids that actually matched instead).
    matched = {s["id"] for s in signals if s.get("outcome")}
    unattributed = []
    for ref, c in closes.items():
        if ref in matched:
            continue
        unattributed.append({
            "id": ref,
            "exit": c.get("exit"),
            "ret_pct": (round(c["ret"] * 100, 2)
                        if isinstance(c.get("ret"), (int, float)) else None),
            "why": c.get("why"),
            "held_days": c.get("held_days"),
            "note": "no committed entry record exists for this close",
        })

    # equity curve from closed signals in sequence
    equity, cum = [], 0.0
    peak = 0.0
    for s in signals:
        o = s.get("outcome")
        if not o or o.get("ret_pct") is None:
            continue
        cum += o["ret_pct"]
        peak = max(peak, cum)
        equity.append({
            "i": len(equity),
            "seq": s["seq"],
            "ts": o.get("closed_ts"),
            "symbol": s["symbol"],
            # per-trade return, so the chart tooltip and the "view as table"
            # listing can show what each point was, not just the total
            "ret_pct": o.get("ret_pct"),
            "cumulative": cum / 100.0,
            "cumulative_pct": cum,
            "drawdown_pct": cum - peak,
            # lets the record toggles filter the chart alongside the table
            "backfilled": bool(s.get("backfilled")),
        })

    # ---- execution status ------------------------------------------------
    # Read from config/execution-status.json so the disclosure travels with
    # the data. The UI review suggested deleting the 'demo-' prefix from the
    # record ids; that would have made the site claim something false, since
    # every signal executed against Bybit's demo endpoint. Instead the status
    # becomes a first-class part of the payload and is stated on every page.
    status_path = Path(__file__).resolve().parents[1] / "config" / "execution-status.json"
    try:
        execution = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        # Never publish silently without the disclosure: if the status file is
        # unreadable we say so rather than implying live trading.
        execution = {
            "simulated": True,
            "venue": "unknown",
            "real_capital_deployed": False,
            "note": f"execution status could not be read ({exc}); assume simulated",
        }

    # ---- cost model ------------------------------------------------------
    # States, from the code, whether fees and slippage are in the published
    # numbers. The site previously claimed they were included; no close record
    # has ever carried a fee, so that claim was false.
    try:
        import record_figures  # noqa: PLC0415
        costs = record_figures.build()
        _ok, _notes = record_figures.verify_against_ledger(
            Path(__file__).resolve().parents[1] / "data" / "signal-ledger.jsonl"
        )
        costs["verified_against_ledger"] = _ok
        costs["verification_notes"] = _notes
    except Exception as exc:  # noqa: BLE001
        costs = {
            "fees_included": False,
            "short": "Price returns, before costs",
            "long": f"cost model unavailable ({exc}); assume no costs are included",
            "verified_against_ledger": False,
        }

    # ---- claim audit -----------------------------------------------------
    # Configured vs observed, checked mechanically. Four false claims have
    # already shipped on this site, each written from intent rather than from
    # the data; this makes the copy answer to the record.
    try:
        import claim_audit  # noqa: PLC0415
        claims = claim_audit.build(
            Path(__file__).resolve().parents[1] / "data" / "signal-ledger.jsonl"
        )
    except Exception as exc:  # noqa: BLE001
        claims = {"claims": [], "summary": f"claim audit unavailable ({exc})"}

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "execution": execution,
        "costs": costs,
        "claims": claims,
        "chain": {
            "records": len(records),
            # A reader who sees "38 records" next to a trade table with 19 rows
            # reasonably concludes the site is inconsistent. It is not: each
            # trade occupies TWO records (a pre-outcome commitment and its
            # outcome), and corrections are records too. Broken out so the
            # arithmetic is visible rather than implied.
            "by_type": {t: sum(1 for r in records if r.get("type") == t)
                        for t in sorted({r.get("type") for r in records})},
            "records_per_trade": 2,
            "explanation": (
                "The chain holds more records than trades. Each trade is two "
                "records: the signal, committed before the outcome was known, "
                "and the outcome appended when it closed. Corrections are a "
                "third kind. This is why the record count and the trade count "
                "differ, and why it is a sign the history is append-only."
            ),
            # One entry per record, in sequence, for the chain strip. Includes
            # corrections, which are real records and part of the append-only
            # story — a reader should see them rather than have them hidden
            # behind a trade count that silently excludes them.
            "strip": [
                {
                    "seq": r.get("seq"),
                    "kind": {"signal": "signal", "close": "outcome",
                             "reconcile": "correction"}.get(r.get("type"), "correction"),
                    "hash": r.get("hash") or "",
                }
                for r in sorted(records, key=lambda x: x.get("seq") or 0)
            ],
            "signals": len(entries_by_id),
            "closes": len(closes),
            "head": sl.chain_head()[1],
            "genesis": "0" * 64,
            "first_ts": records[0].get("ts") if records else None,
            "last_ts": records[-1].get("ts") if records else None,
            "algorithm": "sha256",
            # Stated so a visitor can recompute without reading our source.
            "canonical_form": (
                'sha256(prev_hash + "|" + json.dumps(payload, '
                "sort_keys=True, separators=(',',':')))"
            ),
            # The full chain, downloadable and verifiable offline.
            "verify_url": "/data/ledger.jsonl",
        },
        # ---- THE RECONCILED MODEL (single source of truth) ----------------
        # Every published figure derives from ONE closed-set definition in
        # record_model.py, and check_consistency() asserts the arithmetic
        # holds before this is allowed to publish. This exists because an
        # external review found the stats contradicting the table.
        "stats": model,
        # raw ledger view kept alongside so the two can be cross-checked
        "router_stats": raw_stats,
        "signals": signals,
        # closes with no committed entry — published, never silently dropped
        "unattributed": unattributed,
        "equity_curve": equity,
        "notes": [
            "Every figure derives from one closed-set definition.",
            "/data/ledger.jsonl is the complete chain — verify it offline.",
            "Closes with no committed entry appear under 'unattributed' and are "
            "excluded from the metrics, so the published totals reconcile.",
        ],
    }


def write_full_chain() -> bool:
    """Write every record to /data/ledger.jsonl, byte-stable.

    THIS is what makes verification real. The previous payload shipped only
    signal hashes, so the browser could check a digest was 64 characters but
    could not recompute anything — an external review correctly called that a
    format check, not a chain check. Shipping the whole chain lets a visitor
    rebuild sha256(prev + "|" + record) for all N records themselves, and
    extend the same check offline with any language.
    """
    import signal_ledger as sl

    recs = sl.read_records()
    rendered = "".join(json.dumps(r, sort_keys=True) + "\n" for r in recs)
    target = OUT.parent / "ledger.jsonl"
    before = target.read_text() if target.exists() else ""
    if rendered == before:
        print(f"  unchanged   : {target}")
        return False
    target.write_text(rendered, encoding="utf-8")
    print(f"  written     : {target}  ({len(recs)} records, {len(rendered):,} bytes)")
    return True


def main() -> int:
    push = "--push" in sys.argv

    print("=" * 70)
    print("STATIC LEDGER EXPORT")
    print("=" * 70)

    if not LEDGER.exists():
        print("  ⛔ no ledger found")
        return 2

    payload = build_payload()
    OUT.parent.mkdir(parents=True, exist_ok=True)

    # REFUSE TO PUBLISH INCONSISTENT FIGURES.
    # An external review found the stats disagreeing with the table; this gate
    # makes that class of bug impossible to ship again. Better to serve stale
    # data than numbers that contradict themselves.
    import record_model as rm
    problems = rm.check_consistency(payload["stats"])
    if problems:
        print("  ⛔ NOT PUBLISHING — figures do not reconcile:")
        for p in problems:
            print(f"     - {p}")
        return 2

    write_full_chain()

    before = OUT.read_text() if OUT.exists() else ""
    # stable, pretty-printed output so git diffs are readable and only
    # change when the record actually changes (generated_at excluded)
    body = {k: v for k, v in payload.items() if k != "generated_at"}
    after_body = json.dumps(body, indent=1, sort_keys=True)

    if before:
        try:
            prev = json.loads(before)
            prev_body = json.dumps(
                {k: v for k, v in prev.items() if k != "generated_at"},
                indent=1, sort_keys=True)
            changed = prev_body != after_body
        except Exception:
            changed = True
    else:
        changed = True

    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

    size = OUT.stat().st_size
    print(f"  records     : {payload['chain']['records']}")
    print(f"  signals     : {len(payload['signals'])}")
    print(f"  closed      : {payload['stats'].get('closed')}")
    print(f"  equity pts  : {len(payload['equity_curve'])}")
    print(f"  written     : {OUT}  ({size:,} bytes)")
    print(f"  changed     : {'yes' if changed else 'no (identical record)'}")

    if not push:
        print("\n  (dry run — pass --push to commit and deploy)")
        return 0

    if not changed:
        print("\n  nothing to deploy — record unchanged")
        return 0

    return publish(SITE, payload["chain"]["records"], payload["stats"].get("closed"))


def publish(site: Path, n, closed) -> int:
    """Commit the export and push it. The site repo also receives front-end changes, so this checkout can be
    behind origin: pull --rebase BEFORE the push, or the push is rejected (non-fast-forward) and the public
    record silently stops updating. A rebase conflict aborts the rebase and fails loudly; nothing is forced."""
    bot = ["-c", "user.name=ledger-bot", "-c", "user.email=ledger-bot@users.noreply.github.com"]
    steps = [
        ("add", ["git", "add", "-A"]),
        ("commit", ["git", *bot, "commit", "-q", "-m", f"data: ledger export — {n} records, {closed} closed"]),
        ("pull --rebase", ["git", *bot, "pull", "--rebase", "-q", "origin", "main"]),
        ("push", ["git", "push", "-q", "origin", "main"]),
    ]
    for step, argv in steps:
        p = subprocess.run(argv, cwd=str(site), capture_output=True, text=True, timeout=180)
        if p.returncode != 0 and "nothing to commit" not in (p.stdout + p.stderr):
            if step == "pull --rebase":
                subprocess.run(["git", "rebase", "--abort"], cwd=str(site), capture_output=True, text=True,
                               timeout=60)
            print(f"  ⛔ {step} failed: {(p.stdout + p.stderr).strip()[:200]}")
            return 3
    print("\n  ✅ committed and pushed — Vercel will redeploy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
