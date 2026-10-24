#!/usr/bin/env python3
"""A unified, read-only view of EVERY signal the system produces.

WHAT THE OWNER ASKED FOR
------------------------
"Every signal, no matter the currency or type, like a library, and I need to see
all the progress there." The public site cannot show this: it renders a
hash-chained record where each entry was committed before its outcome was known,
so entries cannot be added retroactively and the seven books below cannot be
merged into it without making it untrue. This is the private view instead. It
reads the books directly and writes nothing.

THE SEVEN SOURCES
-----------------
  signal-ledger.json    regime WATCH/bias signals (72)
  unified-paper.json    the main paper book, multi-family (thousands)
  live-trades.json      real-money spot fills
  pa-testnet-ledger.json testnet DCA entries
  ensemble-paper.json   ensemble paper trades
  v2 scorecards         JEV lane decisions (agreement with the gateway)
  hunt5 findings        net-positive rules found by the standing hunt

THE ONE RULE THIS TOOL HOLDS ITSELF TO
--------------------------------------
Numbers are shown net of a configurable round-trip cost, and the gross figure is
shown beside it. The live paper book is +$18.97 gross and -$43.29 after 0.30%
round trips; a view that reported only the gross number would be the same lie the
site's audit exists to catch. Where a source does not record enough to compute
net (no size, no entry), it is marked "unknown" — never guessed.

EVERY BOOK IS LABELLED BY PROVENANCE
------------------------------------
  REAL      real money moved
  TESTNET   testnet money moved
  PAPER     simulated fills
  BACKTEST  replay over historical bars
  SIGNAL    a proposal, no fill at all

Mixing these without labels is how paper results get mistaken for real ones.

Usage:
    python3 signal_library.py                 # summary + all books
    python3 signal_library.py --book live     # one book
    python3 signal_library.py --json          # machine-readable
    python3 signal_library.py --html out.html # self-contained page
    python3 signal_library.py --open          # serve on 127.0.0.1 and print URL
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
DATA = BASE / "data"
V2 = Path("/opt/trading-agent-v2/data/v2")

# 0.10% fee per side + 0.05% slippage per side = 0.30% round trip
DEFAULT_RT_COST = 0.003

PROVENANCE = {
    "real":    ("REAL",     "real money moved"),
    "testnet": ("TESTNET",  "testnet money moved"),
    "paper":   ("PAPER",    "simulated fills"),
    "backtest": ("BACKTEST", "replay over historical bars"),
    "signal":  ("SIGNAL",   "proposal only, no fill"),
}


@dataclass
class Trade:
    book: str
    provenance: str
    symbol: str
    side: str = ""
    family: str = ""
    timeframe: str = ""
    entry: float | None = None
    exit: float | None = None
    size_usdt: float | None = None
    pnl_gross: float | None = None
    ret_pct: float | None = None
    opened: str = ""
    closed: str = ""
    reason: str = ""
    state: str = "closed"          # closed | open | signal
    source_file: str = ""

    def net(self, rt_cost: float = DEFAULT_RT_COST) -> float | None:
        """Net P&L. None when the record cannot support it — never guessed."""
        if self.pnl_gross is None:
            return None
        # cost applies to both legs of the round trip, on the traded notional
        if self.size_usdt is None:
            return None
        return self.pnl_gross - self.size_usdt * rt_cost


def _f(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _load(name: str):
    p = DATA / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


# ---------------------------------------------------------------- the loaders

def load_signal_ledger() -> list[Trade]:
    """Regime signals: proposals, not fills."""
    d = _load("signal-ledger.json") or {}
    out = []
    for s in d.get("signals", []):
        out.append(Trade(
            book="regime-signals", provenance="signal",
            symbol=s.get("symbol", "?"), side=s.get("bias", ""),
            family=s.get("regime", ""), entry=_f(s.get("price")),
            opened=s.get("ts_utc", ""), state="signal",
            reason=s.get("reason", "")[:80],
            source_file="signal-ledger.json",
        ))
    return out


def load_unified_paper() -> list[Trade]:
    """The main paper book. Both open and closed."""
    d = _load("unified-paper.json") or {}
    out = []
    for t in d.get("closed", []):
        out.append(Trade(
            book="unified-paper", provenance="paper",
            symbol=t.get("symbol", "?"), side=t.get("side", ""),
            family=t.get("edge", ""), timeframe=t.get("tf", ""),
            entry=_f(t.get("entry")), exit=_f(t.get("exit")),
            size_usdt=_f(t.get("size")) or _f(t.get("size_usdt")),
            pnl_gross=_f(t.get("pnl")),
            opened=t.get("ts_utc", "") or t.get("open_ts_utc", ""),
            reason=t.get("reason", "") or t.get("why", ""),
            state="closed", source_file="unified-paper.json",
        ))
    for t in d.get("open", []):
        out.append(Trade(
            book="unified-paper", provenance="paper",
            symbol=t.get("symbol", "?"), side=t.get("side", ""),
            family=t.get("edge", ""), timeframe=t.get("tf", ""),
            entry=_f(t.get("entry")),
            size_usdt=_f(t.get("size")) or _f(t.get("size_usdt")),
            opened=t.get("ts_utc", ""), state="open",
            source_file="unified-paper.json",
        ))
    return out


def load_live_trades() -> list[Trade]:
    """REAL money spot fills — the only book where real capital moved."""
    d = _load("live-trades.json") or {}
    out = []
    for t in d.get("closed", []):
        out.append(Trade(
            book="live-spot", provenance="real",
            symbol=t.get("symbol", "?"), side=t.get("side", ""),
            entry=_f(t.get("entry")), exit=_f(t.get("exit")),
            size_usdt=_f(t.get("size_usdt")),
            pnl_gross=_f(t.get("pnl")),
            opened=t.get("open_ts_utc", ""), closed=t.get("close_ts_utc", ""),
            reason=t.get("reason", ""), state="closed",
            source_file="live-trades.json",
        ))
    for t in d.get("open", []):
        out.append(Trade(
            book="live-spot", provenance="real",
            symbol=t.get("symbol", "?"), side=t.get("side", ""),
            entry=_f(t.get("entry")), size_usdt=_f(t.get("size_usdt")),
            opened=t.get("open_ts_utc", ""), state="open",
            source_file="live-trades.json",
        ))
    return out


def load_testnet() -> list[Trade]:
    d = _load("pa-testnet-ledger.json") or {}
    out = []
    for t in d.get("entries", []):
        out.append(Trade(
            book="pa-testnet", provenance="testnet",
            symbol=t.get("symbol", "?"), side="long",
            family="pa-dca", entry=_f(t.get("price")),
            size_usdt=_f(t.get("amount_usdt")),
            opened=t.get("ts_utc", ""), state="open",
            source_file="pa-testnet-ledger.json",
        ))
    return out


def load_ensemble() -> list[Trade]:
    d = _load("ensemble-paper.json") or {}
    out = []
    for t in d.get("closed", []):
        out.append(Trade(
            book="ensemble-paper", provenance="paper",
            symbol=t.get("symbol", "?"), side=t.get("side", ""),
            family=t.get("family", ""), timeframe=t.get("timeframe", ""),
            entry=_f(t.get("entry")), exit=_f(t.get("exit")),
            size_usdt=_f(t.get("size")), pnl_gross=_f(t.get("pnl")),
            opened=t.get("open_ts_utc", "") or t.get("ts_utc", ""),
            reason=t.get("reason", ""), state="closed",
            source_file="ensemble-paper.json",
        ))
    return out


def load_v2_lanes() -> dict:
    """JEV lanes: decisions, agreement, and the gate values. No trades yet."""
    lanes = {}
    for lane in ("paper", "paper-b"):
        p = V2 / lane / "heartbeat.json"
        if not p.exists():
            continue
        try:
            h = json.loads(p.read_text())
        except Exception:
            continue
        sc = {}
        scp = V2 / lane / "scorecard.json"
        if scp.exists():
            try:
                sc = json.loads(scp.read_text())
            except Exception:
                sc = {}
        lanes[lane] = {
            "cycles": h.get("cycles"),
            "steps": h.get("steps"),
            "jev_ok": h.get("jev_ok"),
            "jev_calls": h.get("jev_calls"),
            "kill_engaged": h.get("kill_engaged"),
            "positions": len(h.get("positions") or []),
            "agreement": sc.get("agreement_jev_x_gateway") or {},
            "pending": sc.get("pending"),
        }
    return lanes


def load_hunt5() -> list[dict]:
    p = BASE / "reports" / "hunt5" / "findings.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def load_certification() -> dict:
    """The walk-forward verdict, if it has been run.

    This is the difference between "a rule that tested well" and "a rule that
    holds in every consecutive period". The hunt's findings are in-sample, best
    variant per rule; the certification fixes the exit in advance and splits the
    history into consecutive folds. Showing only the hunt numbers would present
    every rule at its most flattering, which is what this page exists to avoid.
    """
    for name in ("certification.json", "walkforward.json"):
        p = BASE / "reports" / name
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                continue
    return {}


# ---------------------------------------------------------------- aggregation

@dataclass
class BookSummary:
    book: str
    provenance: str
    trades: int = 0
    closed: int = 0
    open: int = 0
    signals: int = 0
    wins: int = 0
    losses: int = 0
    pnl_gross: float = 0.0
    pnl_net: float = 0.0
    net_known: int = 0          # how many closed trades could be netted
    symbols: set = field(default_factory=set)
    families: set = field(default_factory=set)

    def win_rate(self) -> float | None:
        dec = self.wins + self.losses
        return (self.wins / dec * 100) if dec else None


def summarise(trades: list[Trade], rt_cost: float) -> dict[str, BookSummary]:
    books: dict[str, BookSummary] = {}
    for t in trades:
        b = books.setdefault(t.book, BookSummary(t.book, t.provenance))
        b.trades += 1
        b.symbols.add(t.symbol)
        if t.family:
            b.families.add(t.family)
        if t.state == "closed":
            b.closed += 1
            if t.pnl_gross is not None:
                b.pnl_gross += t.pnl_gross
                if t.pnl_gross > 0:
                    b.wins += 1
                else:
                    b.losses += 1
                n = t.net(rt_cost)
                if n is not None:
                    b.pnl_net += n
                    b.net_known += 1
        elif t.state == "open":
            b.open += 1
        else:
            b.signals += 1
    return books


# ---------------------------------------------------------------- rendering

def render_text(trades, books, lanes, hunts, rt_cost) -> str:
    L = []
    L.append("=" * 78)
    L.append("PRIVATE SIGNAL LIBRARY — every book, every symbol, every state")
    L.append(f"generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
             f"   round-trip cost applied: {rt_cost*100:.2f}%")
    L.append("=" * 78)

    tot_gross = tot_net = 0.0
    L.append("")
    L.append(f"{'book':18} {'kind':9} {'trades':>7} {'closed':>7} {'open':>6} "
             f"{'win%':>6} {'gross':>10} {'net':>10}")
    L.append("-" * 78)
    for name in sorted(books):
        b = books[name]
        kind = PROVENANCE[b.provenance][0]
        wr = f"{b.win_rate():.1f}" if b.win_rate() is not None else "—"
        net_s = f"{b.pnl_net:+.2f}" if b.net_known else "unknown"
        L.append(f"{name:18} {kind:9} {b.trades:>7} {b.closed:>7} {b.open:>6} "
                 f"{wr:>6} {b.pnl_gross:>+10.2f} {net_s:>10}")
        tot_gross += b.pnl_gross
        tot_net += b.pnl_net
    L.append("-" * 78)
    L.append(f"{'TOTAL':18} {'':9} {'':>7} {'':>7} {'':>6} {'':>6} "
             f"{tot_gross:>+10.2f} {tot_net:>+10.2f}")
    L.append("")
    L.append("  gross vs net is the point: fees are charged PER TRADE, so a book")
    L.append("  with a small edge and many trades is negative after costs.")
    L.append("")

    L.append("PER-SYMBOL (all books, closed trades)")
    L.append("-" * 78)
    per = defaultdict(lambda: [0, 0.0, 0.0])
    for t in trades:
        if t.state != "closed" or t.pnl_gross is None:
            continue
        r = per[t.symbol]
        r[0] += 1
        r[1] += t.pnl_gross
        r[2] += (t.net(rt_cost) or 0.0)
    for sym, (n, g, net) in sorted(per.items(), key=lambda kv: -kv[1][0]):
        L.append(f"  {sym:12} n={n:5}  gross {g:+9.2f}  net {net:+9.2f}")
    L.append("")

    if lanes:
        L.append("V2 / JEV LANES")
        L.append("-" * 78)
        for lane, d in lanes.items():
            L.append(f"  {lane:10} cycles={d['cycles']} steps={d['steps']} "
                     f"jev={d['jev_ok']}/{d['jev_calls']} "
                     f"positions={d['positions']} kill={d['kill_engaged']}")
            ag = d.get("agreement") or {}
            if ag:
                top = sorted(ag.items(), key=lambda kv: -kv[1])[:3]
                L.append(f"             agreement: " +
                         ", ".join(f"{k}={v}" for k, v in top))
        L.append("")

    if hunts:
        L.append(f"STANDING HUNT — {len(hunts)} findings on file")
        L.append("-" * 78)
        best = sorted(hunts, key=lambda h: -h.get("dn_pf", 0))[:8]
        for h in best:
            L.append(f"  {h.get('name','?'):26} down n={h.get('dn_n'):>4} "
                     f"PF {h.get('dn_pf'):.2f}  net ${h.get('dn_net'):+.2f}  "
                     f"up ${h.get('up_net'):+.2f}")
        L.append("")

    L.append("PROVENANCE")
    L.append("-" * 78)
    for k, (label, why) in PROVENANCE.items():
        L.append(f"  {label:9} {why}")
    return "\n".join(L)


def render_html(trades, books, lanes, hunts, rt_cost) -> str:
    rows = []
    for name in sorted(books):
        b = books[name]
        kind = PROVENANCE[b.provenance][0]
        wr = f"{b.win_rate():.1f}%" if b.win_rate() is not None else "—"
        net_s = f"{b.pnl_net:+.2f}" if b.net_known else "unknown"
        cls = "pos" if b.pnl_net > 0 else ("neg" if b.net_known else "")
        rows.append(
            f"<tr><td>{html.escape(name)}</td>"
            f"<td><span class='tag {b.provenance}'>{kind}</span></td>"
            f"<td class='n'>{b.trades}</td><td class='n'>{b.closed}</td>"
            f"<td class='n'>{b.open}</td><td class='n'>{wr}</td>"
            f"<td class='n'>{b.pnl_gross:+.2f}</td>"
            f"<td class='n {cls}'>{net_s}</td></tr>")

    per = defaultdict(lambda: [0, 0.0, 0.0])
    for t in trades:
        if t.state != "closed" or t.pnl_gross is None:
            continue
        r = per[t.symbol]
        r[0] += 1
        r[1] += t.pnl_gross
        r[2] += (t.net(rt_cost) or 0.0)
    sym_rows = "".join(
        f"<tr><td>{html.escape(s)}</td><td class='n'>{n}</td>"
        f"<td class='n'>{g:+.2f}</td>"
        f"<td class='n {'pos' if net > 0 else 'neg'}'>{net:+.2f}</td></tr>"
        for s, (n, g, net) in sorted(per.items(), key=lambda kv: -kv[1][0]))

    lane_html = ""
    for lane, d in (lanes or {}).items():
        ag = d.get("agreement") or {}
        top = sorted(ag.items(), key=lambda kv: -kv[1])[:3]
        lane_html += (
            f"<tr><td>{html.escape(lane)}</td><td class='n'>{d['cycles']}</td>"
            f"<td class='n'>{d['steps']}</td>"
            f"<td class='n'>{d['jev_ok']}/{d['jev_calls']}</td>"
            f"<td class='n'>{d['positions']}</td>"
            f"<td class='n'>{d['kill_engaged']}</td>"
            f"<td class='small'>{html.escape(', '.join(f'{k}={v}' for k,v in top))}</td></tr>")

    # --- certification: the honest companion to the hunt findings -------------
    cert_html = ""
    cert = load_certification()
    if cert.get("rules"):
        rows = []
        for name, r in sorted(cert["rules"].items(),
                              key=lambda kv: -kv[1]["total_net"]):
            per = " &middot; ".join(f"{f.get('net', 0):+.1f}" for f in r["folds"])
            ok = r["all_positive"]
            rows.append(
                f"<tr><td>{html.escape(name)}</td>"
                f"<td class='n'>{r['total_n']}</td>"
                f"<td class='n {'pos' if r['total_net'] > 0 else 'neg'}'>"
                f"{r['total_net']:+.2f}</td>"
                f"<td class='n'>{r['positive_folds']}/{r['folds_n']}</td>"
                f"<td class='small'>{per}</td>"
                f"<td class='{'pos' if ok else 'neg'}'>"
                f"{'CERTIFIABLE' if ok else 'not certified'}</td></tr>")
        cert_html = (
            "<h2>Rule certification &mdash; consecutive folds, not the aggregate</h2>"
            "<table><tr><th>rule</th><th>n</th><th>net $</th><th>pos folds</th>"
            "<th>per-fold net</th><th>verdict</th></tr>" + "".join(rows) + "</table>"
            "<div class='note'>The hunt's numbers are in-sample best-variant-per-rule. "
            "These are the same rules with the exit <em>fixed in advance</em> and the "
            "history split into consecutive folds. A rule positive overall but negative "
            "in one fold is not certified &mdash; and every one of these loses in the "
            "same fold, which was the largest rally in the sample. Short-only rules "
            "make money when the market falls.</div>")

    hunt_html = "".join(
        f"<tr><td>{html.escape(h.get('name','?'))}</td>"
        f"<td class='n'>{h.get('dn_n')}</td><td class='n'>{h.get('dn_pf'):.2f}</td>"
        f"<td class='n {'pos' if (h.get('dn_net') or 0) > 0 else 'neg'}'>"
        f"{h.get('dn_net'):+.2f}</td>"
        f"<td class='n {'pos' if (h.get('up_net') or 0) > 0 else 'neg'}'>"
        f"{h.get('up_net'):+.2f}</td></tr>"
        for h in sorted(hunts, key=lambda h: -h.get("dn_pf", 0))[:15])

    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Private Signal Library</title>
<style>
:root{{--bg:#0e1116;--fg:#e6edf3;--dim:#8b949e;--line:#21262d;--pos:#3fb950;--neg:#f85149}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0;padding:32px}}
h1{{font-size:18px;margin:0 0 4px}} h2{{font-size:14px;margin:28px 0 8px;color:var(--dim);
text-transform:uppercase;letter-spacing:.08em}}
.meta{{color:var(--dim);font-size:12px;margin-bottom:20px}}
table{{border-collapse:collapse;width:100%;margin-bottom:8px}}
th{{text-align:left;color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase;
letter-spacing:.06em;border-bottom:1px solid var(--line);padding:6px 10px}}
td{{padding:6px 10px;border-bottom:1px solid var(--line)}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
.pos{{color:var(--pos)}} .neg{{color:var(--neg)}} .small{{font-size:11px;color:var(--dim)}}
.tag{{font-size:10px;padding:2px 6px;border-radius:3px;letter-spacing:.06em}}
.tag.real{{background:#3d1d1d;color:#ff9b9b}} .tag.testnet{{background:#1d2d3d;color:#9bc9ff}}
.tag.paper{{background:#1d3d2a;color:#9bffc4}} .tag.backtest{{background:#2d2d1d;color:#e8e89b}}
.tag.signal{{background:#2d1d3d;color:#d49bff}}
.note{{color:var(--dim);font-size:12px;margin-top:20px;border-top:1px solid var(--line);padding-top:12px}}
</style></head><body>
<h1>Private Signal Library</h1>
<div class="meta">generated {gen} &middot; round-trip cost {rt_cost*100:.2f}% applied to net
&middot; read-only, no public exposure</div>

<h2>Books</h2>
<table><tr><th>book</th><th>kind</th><th>trades</th><th>closed</th><th>open</th>
<th>win%</th><th>gross $</th><th>net $</th></tr>{''.join(rows)}</table>

<h2>Per symbol (closed, all books)</h2>
<table><tr><th>symbol</th><th>n</th><th>gross $</th><th>net $</th></tr>{sym_rows}</table>

<h2>V2 / JEV lanes</h2>
<table><tr><th>lane</th><th>cycles</th><th>steps</th><th>jev</th><th>positions</th>
<th>kill</th><th>agreement</th></tr>{lane_html}</table>

{cert_html}

<h2>Standing hunt findings (in-sample)</h2>
<table><tr><th>rule</th><th>down n</th><th>down PF</th><th>down net $</th>
<th>up net $</th></tr>{hunt_html}</table>

<div class="note">
Gross is what the fills produced; net subtracts {rt_cost*100:.2f}% round trip on every
trade. A book with a small edge and thousands of trades is negative after costs — that
is the live paper book's actual result, not a rounding artefact. Trades whose records
cannot support a net figure are shown as <em>unknown</em>, never estimated.
</div>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--cost", type=float, default=DEFAULT_RT_COST)
    args = ap.parse_args()

    trades: list[Trade] = []
    for fn in (load_signal_ledger, load_unified_paper, load_live_trades,
               load_testnet, load_ensemble):
        try:
            trades.extend(fn())
        except Exception as e:
            print(f"warn: {fn.__name__} failed: {e}", file=sys.stderr)

    if args.book:
        trades = [t for t in trades if t.book == args.book]

    books = summarise(trades, args.cost)
    lanes = load_v2_lanes()
    hunts = load_hunt5()

    if args.json:
        print(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "round_trip_cost": args.cost,
            "books": {k: {**asdict(v), "symbols": sorted(v.symbols),
                          "families": sorted(v.families),
                          "win_rate_pct": v.win_rate()}
                      for k, v in books.items()},
            "lanes": lanes,
            "hunt_findings": len(hunts),
        }, indent=2))
        return 0

    page = render_html(trades, books, lanes, hunts, args.cost)
    if args.html:
        Path(args.html).write_text(page)
        print(f"written {args.html}")
        return 0
    if args.open:
        import http.server, socketserver, threading, webbrowser
        port = 8123
        body = page.encode()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):  # noqa: A002 - matches base signature
                pass

        url = f"http://127.0.0.1:{port}/"
        print(f"serving {url}  (127.0.0.1 only — not reachable from outside)")
        with socketserver.TCPServer(("127.0.0.1", port), H) as srv:
            srv.serve_forever()
        return 0

    print(render_text(trades, books, lanes, hunts, args.cost))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
