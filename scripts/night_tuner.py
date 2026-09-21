#!/usr/bin/env python3
"""Night Tuner — autonomous overnight system update + trade monitor.

Runs nightly (00:20 UTC) while the market is monitored. Job:
  1. ANALYZE today's paper+real trades per edge/tf/symbol (rolling 24h + 72h)
  2. DETECT flaws: decaying edges, SL clusters, WR drops, negative EV buckets
  3. ADJUST: safely nudge risk knobs in live-small-account.env within HARD
     bounds (never outside the owner's risk envelope), kill/pause bad edges
  4. MONITOR: verify cron alive, positions sane, no stalls
  5. REPORT: compact morning summary (stdout → Telegram digest)

Zero tokens: pure python, cron-invoked. Owner mandate (2026-09-06): "run full
updates overnight, monitor trades, think of flaws, adapt to any flaw".
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ENV = ROOT / "config" / "live-small-account.env"
REPORT = ROOT / "reports" / "night-tuner.json"

# Hard bounds — the tuner may NEVER exceed these (owner risk envelope)
HARD = {
    "LIVE_STOP_PCT": (0.008, 0.03),      # 0.8% .. 3.0%
    "LIVE_TP_RR": (1.2, 3.0),            # 1.2 .. 3.0 reward:risk
    "LIVE_HOLD_HOURS": (4.0, 24.0),
    "LIVE_MAX_DAILY_LOSS": (1.0, 5.0),
}
STEP = {
    "LIVE_STOP_PCT": 0.002,              # ±0.2% per night max
    "LIVE_TP_RR": 0.2,
}


def _load(p: Path, default):
    try:
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return default


def _stats(rows: list) -> dict | None:
    if not rows:
        return None
    w = sum(1 for t in rows if t.get("pnl", 0) > 0)
    return {
        "n": len(rows),
        "win_rate": round(w / len(rows) * 100, 1),
        "net": round(sum(t.get("pnl", 0) for t in rows), 4),
    }


def analyze() -> dict:
    paper = _load(DATA / "unified-paper.json", {"open": [], "closed": []})
    live = _load(DATA / "live-trades.json", {"open": [], "closed": []})
    p_closed = paper.get("closed", [])
    l_closed = live.get("closed", [])
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    # rolling windows
    def _recent(rows, hours):
        out = []
        for t in rows:
            ts = t.get("close_ts_utc") or t.get("ts_utc") or ""
            try:
                dt = datetime.fromisoformat(ts)
            except Exception:
                continue
            if (now - dt).total_seconds() <= hours * 3600:
                out.append(t)
        return out

    p24 = _recent(p_closed, 24)
    p72 = _recent(p_closed, 72)
    l24 = _recent(l_closed, 24)

    report = {
        "ts": now.isoformat(),
        "paper_24h": _stats(p24),
        "paper_72h": _stats(p72),
        "live_24h": _stats(l24),
        "adjustments": [],
        "flaws_found": [],
        "positions": len(live.get("open", [])),
        "cron_alive": _cron_check(),
    }

    # ---- FLAW DETECTION: per-edge 72h stats (need >= 6 trades to judge) ----
    by_edge = defaultdict(list)
    for t in p72:
        e = t.get("edge") or ("native" if not t.get("imported") else "imported")
        by_edge[e].append(t)
    for e, rows in sorted(by_edge.items(), key=lambda x: -len(x[1])):
        st = _stats(rows)
        if not st or st["n"] < 6:
            continue
        verdict = "OK"
        if st["win_rate"] < 42 and st["net"] < 0:
            verdict = "WEAK"
            report["flaws_found"].append(
                f"edge '{e}' decaying: WR {st['win_rate']}% net {st['net']:+.3f} over {st['n']}")
        elif st["win_rate"] >= 60 and st["net"] > 0:
            verdict = "STRONG"
        report.setdefault("edges", []).append({**st, "edge": e, "verdict": verdict})

    # ---- ADJUSTMENTS (only when data supports, tiny steps, hard-clamped) ----
    env_text = ENV.read_text() if ENV.exists() else ""

    def _env_float(key, dflt):
        m = re.search(rf"^{key}\s*=\s*([\d.]+)", env_text, re.M)
        return float(m.group(1)) if m else dflt

    # stop: if live 72h WR ok but avg loss too big vs win, widen slightly;
    # if SL-heavy (WR<45%), tighten slightly to cut bleed
    p_all = _stats(p72) or {}
    if p_all.get("n", 0) >= 15 and p_all["win_rate"] < 50 and p_all["net"] < 0:
        cur = _env_float("LIVE_STOP_PCT", 0.015)
        new = max(HARD["LIVE_STOP_PCT"][0], cur - STEP["LIVE_STOP_PCT"])
        if new < cur:
            env_text = re.sub(r"(LIVE_STOP_PCT\s*=\s*)[\d.]+",
                              rf"\g<1>{new:.4f}", env_text)
            report["adjustments"].append(
                f"tightened LIVE_STOP_PCT {cur} → {new} (WR {p_all['win_rate']}% < 50%)")
    # if winners are getting cut early (high WR but low net), widen TP RR
    if p_all.get("n", 0) >= 15 and p_all["win_rate"] >= 58 and p_all["net"] < 0.05:
        cur = _env_float("LIVE_TP_RR", 2.0)
        new = min(HARD["LIVE_TP_RR"][1], cur + STEP["LIVE_TP_RR"])
        if new > cur:
            env_text = re.sub(r"(LIVE_TP_RR\s*=\s*)[\d.]+",
                              rf"\g<1>{new:.1f}", env_text)
            report["adjustments"].append(
                f"raised LIVE_TP_RR {cur} → {new} (winners cut early)")

    if env_text:
        ENV.write_text(env_text)

    # ---- SUGGESTIONS: always emit 1-3 concrete ideas (owner: 'always suggest
    # updates, run by yourself') — ranked by expected value ----
    suggestions = _suggest(report)
    report["suggestions"] = suggestions

    # ---- KNOWLEDGE-EXPANSION: hot movers we're not yet trading ----
    report["expansion"] = _expansion_scan()

    # ---- MONITOR: stall check ----
    report["watch"] = _watch_check()
    return report


def _suggest(rep: dict) -> list:
    """Zero-token suggestion engine — concrete next-step ideas ranked by impact.
    Fires on data patterns; every suggestion is actionable by the next nightly
    run or by the agent improvement pass."""
    out = []
    edges = rep.get("edges", [])
    strong = [e for e in edges if e.get("verdict") == "STRONG"]
    weak = [e for e in edges if e.get("verdict") == "WEAK"]

    # 1) promotion candidates: strong edges with enough trades
    for e in strong:
        if e["n"] >= 10 and e["win_rate"] >= 60:
            out.append({"rank": 1, "idea": f"PROMOTE '{e['edge']}' (WR {e['win_rate']}%, n={e['n']}) — raise its size/priority toward real money"})
    # 2) kill/flag candidates
    for e in weak:
        out.append({"rank": 2, "idea": f"KILL or pause '{e['edge']}' (WR {e['win_rate']}%, net {e['net']:+.3f}) — it's bleeding"})
    # 3) real-money health
    l24 = rep.get("live_24h") or {}
    if l24.get("n", 0) >= 3 and (l24.get("win_rate", 100) < 45 or l24.get("net", 0) < 0):
        out.append({"rank": 3, "idea": f"Real money cold ({l24.get('n')} trades, WR {l24.get('win_rate')}%, net ${l24.get('net'):+.3f}) — widen stops or stand down until pattern improves"})
    # 4) positive reinforcement: keep doing what works
    p72 = rep.get("paper_72h") or {}
    if p72.get("win_rate", 0) >= 55:
        out.append({"rank": 4, "idea": f"Paper healthy (WR {p72['win_rate']}% over {p72['n']}) — keep cadence, let compounding work"})
    # 5) nothing notable → generic watch
    if not out:
        out.append({"rank": 5, "idea": "No strong signal yet — accumulate trades, re-evaluate tomorrow"})
    return sorted(out, key=lambda x: x["rank"])[:3]


def _expansion_scan() -> list:
    """KNOWLEDGE-EXPANSION SCAN (owner: 'always let me know if we could expand
    to other coins/stocks/memes'). Each night, checks a wide meme/alt universe
    for big movers (>8% 24h with real volume) we're NOT yet trading, so the
    morning digest can suggest additions to the watchlist. Pure ccxt, zero
    tokens. Stocks are NOT possible (Binance is crypto-only, spot keys)."""
    out = []
    try:
        sys.path.insert(0, str(ROOT / "src"))
        sys.path.insert(0, str(ROOT / "scripts"))
        from trading.exchange.live_client import make_live_exchange
        ex = make_live_exchange()
        universe = ["PEPE/USDT", "SHIB/USDT", "WIF/USDT", "FLOKI/USDT",
                    "BONK/USDT", "DOGS/USDT", "NOT/USDT", "ONDO/USDT",
                    "JUP/USDT", "AAVE/USDT", "NEAR/USDT", "APT/USDT",
                    "ARB/USDT", "OP/USDT", "INJ/USDT", "TIA/USDT",
                    "SEI/USDT", "SUI/USDT", "LDO/USDT", "RUNE/USDT"]
        for s in universe:
            try:
                t = ex.fetch_ticker(s)
                chg = float(t.get("percentage", 0) or 0)
                vol = float(t.get("quoteVolume", 0) or 0)
                if chg >= 8.0 and vol >= 5e6:
                    out.append(f"{s} {chg:+.1f}% (vol ${vol/1e6:.0f}M)")
            except Exception:
                continue
    except Exception:
        pass
    return sorted(out)[:5]


def _cron_check() -> str:
    try:
        r = os.popen("crontab -l 2>/dev/null | grep -cE '^\\*/|^[0-9]'").read().strip()
        return f"{r} jobs"
    except Exception:
        return "?"


def _watch_check() -> list:
    """Verify the core supervised processes are alive via their log mtimes."""
    out = []
    checks = {
        "unified-trader": ROOT / "logs" / "unified.log",
    }
    now = datetime.now(timezone.utc).timestamp()
    for name, path in checks.items():
        try:
            age_min = (now - path.stat().st_mtime) / 60
            out.append(f"{name}: {age_min:.0f}m ago" if age_min < 120
                       else f"{name}: STALE {age_min:.0f}m")
        except Exception:
            out.append(f"{name}: no log")
    return out


def main() -> None:
    rep = analyze()
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(rep, indent=2))

    lines = ["🌙 NIGHT TUNER — overnight update complete"]
    if rep.get("paper_24h"):
        p = rep["paper_24h"]
        lines.append(f"Paper 24h: {p['n']} trades · WR {p['win_rate']}% · net ${p['net']:+.3f}")
    if rep.get("live_24h"):
        l = rep["live_24h"]
        lines.append(f"Real 24h: {l['n']} trades · WR {l['win_rate']}% · net ${l['net']:+.3f}")
    if rep.get("edges"):
        for e in rep["edges"][:5]:
            lines.append(f"  • {e['edge']}: WR {e['win_rate']}% · net ${e['net']:+.3f} ({e['verdict']})")
    if rep.get("flaws_found"):
        lines.append("Flaws found:")
        for f in rep["flaws_found"][:3]:
            lines.append(f"  ⚠ {f}")
    else:
        lines.append("No decaying edges detected ✅")
    if rep.get("adjustments"):
        lines.append("Auto-adjustments applied:")
        for a in rep["adjustments"]:
            lines.append(f"  🔧 {a}")
    else:
        lines.append("No adjustments needed (data within healthy bounds)")
    if rep.get("suggestions"):
        lines.append("Suggestions for next pass:")
        for s in rep["suggestions"]:
            lines.append(f"  💡 {s['idea']}")
    if rep.get("expansion"):
        lines.append("🚀 Expansion watch (movers we don't trade yet):")
        for x in rep["expansion"]:
            lines.append(f"  • {x}")
    lines.append(f"Positions open: {rep['positions']} · cron: {rep['cron_alive']}")
    lines.append("Watch: " + ", ".join(rep.get("watch", [])))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
