#!/usr/bin/env python3
"""Auto-improve pass: compute per-edge stats (paper 72h/24h) + live aggregate."""
import json, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/trading-agent")

def load(p):
    try:
        return json.loads((ROOT / p).read_text())
    except Exception as e:
        print(f"ERR load {p}: {e}")
        return {}

def edge_of(t):
    e = t.get("edge")
    if e:
        return e
    if t.get("imported"):
        return "imported"
    if t.get("loose"):
        return "loose"
    return "native"

def recent(rows, hours, now):
    out = []
    for t in rows:
        ts = t.get("close_ts_utc") or t.get("ts_utc") or ""
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if (now - dt).total_seconds() <= hours * 3600 and (now - dt).total_seconds() >= -3600:
            out.append(t)
    return out

def stats(rows):
    if not rows:
        return None
    w = sum(1 for t in rows if t.get("pnl", 0) > 0)
    net = sum(t.get("pnl", 0) for t in rows)
    return {"n": len(rows), "wr": round(w/len(rows)*100, 1),
            "net": round(net, 4), "avg": round(net/len(rows), 4)}

now = datetime.now(timezone.utc)
print(f"NOW: {now.isoformat()}")

paper = load("data/unified-paper.json")
live = load("data/live-trades.json")
p_closed = paper.get("closed", [])
l_closed = live.get("closed", [])
l_open = live.get("open", [])
print(f"paper closed total={len(p_closed)}  live closed total={len(l_closed)}  live open={len(l_open)}")

for h in (24, 72, 168):
    st = stats(recent(p_closed, h, now))
    print(f"PAPER {h}h: {st}")
for h in (24, 72, 168):
    st = stats(recent(l_closed, h, now))
    print(f"LIVE  {h}h: {st}")

print("\n--- PAPER per-edge 72h ---")
p72 = recent(p_closed, 72, now)
by_edge = defaultdict(list)
for t in p72:
    by_edge[edge_of(t)].append(t)
for e, rows in sorted(by_edge.items(), key=lambda x: -len(x[1])):
    st = stats(rows)
    syms = sorted({t.get("symbol","?") for t in rows})
    print(f"{e:12s} n={st['n']:3d} wr={st['wr']:5.1f}% net={st['net']:+8.4f} avg={st['avg']:+.4f} syms={syms}")

print("\n--- PAPER per-edge 24h ---")
p24 = recent(p_closed, 24, now)
by_edge24 = defaultdict(list)
for t in p24:
    by_edge24[edge_of(t)].append(t)
for e, rows in sorted(by_edge24.items(), key=lambda x: -len(x[1])):
    st = stats(rows)
    print(f"{e:12s} n={st['n']:3d} wr={st['wr']:5.1f}% net={st['net']:+8.4f}")

print("\n--- PAPER edges with n>=8 over 168h (all-time recent context) ---")
p168 = recent(p_closed, 168, now)
by_e = defaultdict(list)
for t in p168:
    by_e[edge_of(t)].append(t)
for e, rows in sorted(by_e.items(), key=lambda x: -len(x[1])):
    if len(rows) >= 8:
        st = stats(rows)
        print(f"{e:12s} n={st['n']:3d} wr={st['wr']:5.1f}% net={st['net']:+8.4f}")

print("\n--- pullback recency check (any trades post-kill?) ---")
pb = [t for t in p_closed if edge_of(t) == "pullback"]
pb.sort(key=lambda t: t.get("close_ts_utc") or t.get("ts_utc") or "")
for t in pb[-4:]:
    print(t.get("close_ts_utc") or t.get("ts_utc"), t.get("symbol"), t.get("tf"), "pnl", t.get("pnl"))
pb_open = [p for p in paper.get("open", []) if edge_of(p) == "pullback"]
print("pullback OPEN now:", len(pb_open))
for p in pb_open:
    print("  ", p.get("symbol"), p.get("tf"), p.get("ts_utc"))

print("\n--- LIVE closed detail (72h) ---")
l72 = recent(l_closed, 72, now)
for t in sorted(l72, key=lambda x: x.get("close_ts_utc","")):
    print(t.get("close_ts_utc","")[:16], t.get("symbol"), t.get("reason"), f"pnl={t.get('pnl'):+.4f}")
print("\n--- LIVE open now ---")
for p in l_open:
    print(p.get("symbol"), p.get("tf","?"), "entry", p.get("entry"), "tp", p.get("tp"), "sl", p.get("sl"), "open", p.get("open_ts_utc"))

# live per-edge via matching closed trade symbol/timeframe to paper edge tags? Not reliable; just show open symbols for edge-check
print("\n--- imported edges firing (open paper, last 2h) ---")
for p in paper.get("open", []):
    print(f"  open paper: {p.get('symbol')} {p.get('tf')} edge={p.get('edge')} imported={p.get('imported')} loose={p.get('loose')}")
