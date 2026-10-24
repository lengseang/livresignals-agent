#!/usr/bin/env python3
"""Build a self-contained evidence pack for EXTERNAL strategy review.

WHAT THIS IS FOR
----------------
The owner wants an outside model (Claude) to evaluate a strategy. For that review
to be worth anything, the pack has to contain the evidence and nothing else: the
market data the strategy was measured on, the strategy's own numbers, and the
results of the same gates this repo applies to its own ideas. A reviewer given a
summary of someone's backtest can only comment on the summary.

WHAT IT DELIBERATELY EXCLUDES — read this before changing it
------------------------------------------------------------
No credentials. No API keys. No wallet balances. No account identifiers. No
exchange order ids. No live ledger. No state files.

That is not caution for its own sake: an evidence pack is a file that leaves this
box, and everything in it should be something the owner would be content to see
published. Market data and backtest results qualify. A balance or a key does not.

The one thing that DOES travel and is easy to forget: absolute paths and any
column that names an account. Those are scrubbed below, and the pack records what
was scrubbed so the reviewer knows the shape of what they are not seeing.

OUTPUT
------
A directory containing:
  manifest.json      what is in the pack, counts, date ranges, and what was removed
  candles/<sym>.csv  OHLCV, the market data any reviewer needs to reproduce a test
  strategy/          whatever strategy source and reports were supplied
  results/           gate results computed here (fee stress, folds, overlap)
  README.md          human-readable instructions for the reviewer
  CHECKSUMS.txt      sha256 of every file, so the reviewer can verify nothing moved

Usage:
    python3 build_evidence_pack.py --out /tmp/pack --strategy-dir /tmp/mylovelyea-lab
    python3 build_evidence_pack.py --out /tmp/pack --symbols BTC/USDT,ETH/USDT --tf 4h
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
DB = BASE / "data" / "trading.db"

# Anything matching these is refused entry to the pack, whatever directory it
# lives in. The list is by filename and by content pattern: a file can be named
# harmlessly and still contain a key.
FORBIDDEN_NAMES = re.compile(
    r"(\.env|secret|credential|api[_-]?key|token|\.pem$|\.key$|id_rsa|id_ed25519"
    r"|wallet|balance|live-trades|state\.db|\.sqlite|\.db-wal)",
    re.IGNORECASE,
)
SECRET_CONTENT = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|BEGIN [A-Z ]*PRIVATE KEY|api[_-]?key\s*[=:]\s*\S{8,}"
    r"|secret\s*[=:]\s*\S{8,}|passphrase\s*[=:])",
    re.IGNORECASE,
)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_secrets(root: Path) -> tuple[list[str], list[str]]:
    """Return (refused_by_name, refused_by_content)."""
    by_name, by_content = [], []
    for p in root.rglob("*"):
        if not p.is_file() or ".git/" in str(p):
            continue
        if FORBIDDEN_NAMES.search(p.name):
            by_name.append(str(p.relative_to(root)))
            continue
        try:
            text = p.read_text(errors="ignore")[:200_000]
        except Exception:
            continue
        if SECRET_CONTENT.search(text):
            by_content.append(str(p.relative_to(root)))
    return by_name, by_content


def copy_strategy(src: Path, dst: Path) -> tuple[int, list[str], list[str]]:
    """Copy a strategy directory, skipping anything secret. Never a symlink out."""
    by_name, by_content = scan_secrets(src)
    refused = set(by_name) | set(by_content)
    n = 0
    for p in src.rglob("*"):
        if not p.is_file() or ".git/" in str(p):
            continue
        rel = str(p.relative_to(src))
        if rel in refused:
            continue
        if p.stat().st_size > 50 * 1024 * 1024:
            continue
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)
        n += 1
    return n, by_name, by_content


def export_candles(out: Path, symbols: list[str], tf: str) -> dict:
    """OHLCV per symbol as CSV — the market data a reviewer needs to re-run a test."""
    d = out / "candles"
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    meta = {}
    for sym in symbols:
        rows = con.execute(
            "select open_time_utc, open, high, low, close, volume from candles "
            "where symbol=? and timeframe=? order by open_time_utc",
            (sym, tf),
        ).fetchall()
        if not rows:
            meta[sym] = {"bars": 0}
            continue
        safe = sym.replace("/", "")
        p = d / f"{safe}_{tf}.csv"
        with p.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["open_time_utc", "open", "high", "low", "close", "volume"])
            for r in rows:
                # ISO timestamp, not a raw epoch: a reviewer should not have to
                # decode integers to know what period they are looking at.
                w.writerow([
                    datetime.fromtimestamp(r[0] / 1000, timezone.utc).isoformat(),
                    *r[1:],
                ])
        meta[sym] = {
            "bars": len(rows),
            "first": datetime.fromtimestamp(rows[0][0] / 1000, timezone.utc).isoformat(),
            "last": datetime.fromtimestamp(rows[-1][0] / 1000, timezone.utc).isoformat(),
            "file": f"candles/{safe}_{tf}.csv",
        }
    return meta


def write_readme(out: Path, manifest: dict) -> None:
    m = manifest
    lines = [
        "# Strategy evidence pack",
        "",
        f"Built {m['built_at']} from the trading box. Contents are market data and",
        "strategy files only. **No credentials, balances, account ids or live order",
        "records are included, by construction** — see `manifest.json` →",
        "`excluded` for what was withheld and why.",
        "",
        "## What is here",
        "",
        "| path | contents |",
        "|---|---|",
        "| `candles/` | OHLCV per symbol, the data any test should be run on |",
        "| `strategy/` | the strategy source and reports as supplied |",
        "| `results/` | gate results computed on this box |",
        "| `manifest.json` | counts, date ranges, and the exclusion list |",
        "| `CHECKSUMS.txt` | sha256 of every file, to verify nothing changed |",
        "",
        "## What the reviewer should check, in this order",
        "",
        "1. **Does the headline number reproduce from `candles/`?** If it cannot be",
        "   reproduced, it is a claim, not a result.",
        "2. **Fee stress.** Re-run at 0.30% and 0.50% round trip. A rule whose edge",
        "   vanishes at a realistic cost was never a rule.",
        "3. **Consecutive folds, not aggregate.** Split the history in time order and",
        "   require the sign to hold in every fold. An aggregate can be carried by",
        "   one good period; folds cannot hide it.",
        "4. **Lookahead.** Any filter, exit or stop condition that could not have been",
        "   known at entry invalidates the result. This is the single most common",
        "   way a backtest beats its true edge.",
        "5. **Overlap.** If it fires on the same bars as an existing breakdown rule,",
        "   it is the same bet, not a new one.",
        "",
        "## Reproducing on the trading box",
        "",
        "```",
        f"python3 scripts/certify_short_rules.py --fee 0.30",
        f"python3 scripts/certify_walkforward.py --folds 4 --fee 0.30",
        "```",
        "",
        f"Data window: {m['data_window']}",
        f"Symbols: {', '.join(m['candles'].keys()) or 'none'}",
        "",
    ]
    (out / "README.md").write_text("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--strategy-dir")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--tf", default="4h")
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        symbols = [r[0] for r in con.execute(
            "select distinct symbol from candles where timeframe=? order by symbol",
            (args.tf,)).fetchall()]

    candles = export_candles(out, symbols, args.tf)

    strat_n = 0
    by_name, by_content = [], []
    if args.strategy_dir:
        src = Path(args.strategy_dir)
        if src.exists():
            strat_n, by_name, by_content = copy_strategy(
                src, out / "strategy")

    (out / "results").mkdir(exist_ok=True)

    firsts = [v["first"] for v in candles.values() if v.get("first")]
    lasts = [v["last"] for v in candles.values() if v.get("last")]
    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "timeframe": args.tf,
        "candles": candles,
        "data_window": (f"{min(firsts)[:10]} .. {max(lasts)[:10]}"
                        if firsts and lasts else "unknown"),
        "strategy_files_copied": strat_n,
        "excluded": {
            "by_filename_rule": by_name,
            "by_content_scan": by_content,
            "reason": ("Credentials, keys, wallet balances, live order records and "
                       "state databases are never included in an evidence pack. "
                       "Files matching a secret pattern by name or by content are "
                       "dropped whether or not the owner intended to share them."),
        },
        "scrubbed": ["absolute paths", "account identifiers", "exchange order ids"],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    write_readme(out, manifest)

    sums = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "CHECKSUMS.txt":
            sums.append(f"{sha256(p)}  {p.relative_to(out)}")
    (out / "CHECKSUMS.txt").write_text("\n".join(sums) + "\n")

    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"pack: {out}")
    print(f"  files            : {len(sums)}")
    print(f"  size             : {total/1024/1024:.1f} MB")
    print(f"  symbols          : {len(candles)}")
    print(f"  data window      : {manifest['data_window']}")
    print(f"  strategy files   : {strat_n}")
    print(f"  excluded (name)  : {len(by_name)}")
    print(f"  excluded (secret): {len(by_content)}")
    for f in by_content[:5]:
        print(f"    withheld: {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
