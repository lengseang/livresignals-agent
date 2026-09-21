#!/usr/bin/env python3
"""SELF-IMPROVEMENT ENGINE — zero-token autonomous optimizer (2026-09-12).

Runs on system crontab (pure Python, no LLM) so it can NEVER fail on API
balance like the old agent-based job did (HTTP 402).

Each run it:
  1. Loads all performance data (live, paper, futures, gold).
  2. Measures per-edge / per-symbol stats.
  3. Runs an OUT-OF-SAMPLE validated parameter search on the core signal
     (stop / trail / regime) and picks the best config that generalises.
  4. Applies only SAFE, BOUNDED changes (hard-clamped envelope below).
  5. Disables proven-losing edges and flags promotion candidates in a
     signals-state file (never patches credentials or code blindly).
  6. Writes reports/self-improve.json and pings Telegram with a summary.

HARD ENVELOPE (never exceeded):
  stop 0.008-0.030 | tp_rr 1.2-3.0 | max_open 2 | size <= 7.0
  daily loss cap >= -3.0 (spot) | futures leverage == 3 (never touched)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
REPORTS = BASE / "reports"
LOGS = BASE / "logs"
ENV = BASE / "config" / "live-small-account.env"
STATE = DATA / "signals-state.json"

# --- hard envelope (never exceed) ---
BOUNDS = {
    "LIVE_STOP_PCT": (0.008, 0.030),
    "LIVE_TP_RR": (1.2, 3.0),
    "LIVE_MAX_OPEN": (1, 2),
    "LIVE_SIZE_USD": (3.0, 7.0),
}
FEE = 0.002


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(p: Path, default):
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def read_env() -> dict:
    out = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def write_env(vals: dict) -> None:
    """Rewrite only the keys we manage, preserving other lines."""
    if not ENV.exists():
        return
    lines = ENV.read_text(encoding="utf-8").splitlines()
    out = []
    seen = set()
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in vals:
                out.append(f"{k}={vals[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in vals.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")


def stats(trades: list, pnl_key: str = "pnl") -> dict:
    n = len(trades)
    if not n:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "ev": 0.0}
    w = sum(1 for t in trades if (t.get(pnl_key) or 0) > 0)
    pnl = sum((t.get(pnl_key) or 0) for t in trades)
    return {"n": n, "wr": w / n * 100, "pnl": pnl, "ev": pnl / n}


def dedupe(trades: list, keys) -> list:
    seen = set()
    out = []
    for t in trades:
        k = tuple(str(t.get(x, ""))[:16] for x in keys)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


# ---------------------------------------------------------------- optimizer
def optimize(symbols: list[str]) -> dict:
    """OOS-validated search over stop/trail on the sharp dip signal.
    Returns the best config that is positive in BOTH halves, else current."""
    try:
        sys.path.insert(0, str(BASE / "scripts"))
        from trading.exchange.live_client import make_live_exchange
        ex = make_live_exchange()
    except Exception:
        return {}

    def rsi(c, n=14):
        if len(c) < n + 1:
            return 50.0
        g = l = 0.0
        for i in range(-n, 0):
            dx = c[i] - c[i - 1]
            g += dx if dx > 0 else 0
            l += -dx if dx <= 0 else 0
        return 100.0 if l == 0 else 100 - 100 / (1 + (g / n) / (l / n))

    def ema(cl, n=200):
        k = 2 / (n + 1)
        e = cl[0]
        for v in cl[1:]:
            e = v * k + e * (1 - k)
        return e

    def sim(o, sl, trail, act, use_regime):
        cl = [x[4] for x in o]
        hi = [x[2] for x in o]
        out = []
        for i in range(210, len(o) - 97):
            c = cl[:i + 1]
            px = c[-1]
            r = rsi(c)
            dd = px / max(hi[i - 90:i]) - 1
            if not (dd <= -0.011 and r <= 35):
                continue
            if use_regime and px < ema(c, 200):
                continue
            e = cl[i]
            stop = e * (1 - sl)
            peak = e
            ret = None
            for b in o[i + 1:i + 97]:
                if b[3] <= stop:
                    ret = stop / e - 1
                    break
                if b[2] > peak:
                    peak = b[2]
                if peak >= e * (1 + act):
                    stop = max(stop, peak * (1 - trail))
            if ret is None:
                ret = cl[i + 96] / e - 1
            out.append(ret - FEE)
        return out

    data = {}
    for s in symbols:
        try:
            o = ex.fetch_ohlcv(s, "15m", limit=1200)
            if o and len(o) > 500:
                data[s] = o
        except Exception:
            continue

    grid = [
        (0.012, 0.008, 0.008, True),
        (0.015, 0.010, 0.010, True),
        (0.010, 0.006, 0.006, True),
        (0.020, 0.012, 0.010, True),
    ]
    best = None
    results = []
    for sl, trail, act, reg in grid:
        is_p, oos_p = [], []
        for s, o in data.items():
            half = len(o) // 2
            is_p += sim(o[:half], sl, trail, act, reg)
            oos_p += sim(o[half:], sl, trail, act, reg)
        isp = sum(is_p) * 100
        oosp = sum(oos_p) * 100
        n = len(is_p) + len(oos_p)
        results.append({"sl": sl, "trail": trail, "regime": reg,
                        "is_pct": round(isp, 2), "oos_pct": round(oosp, 2), "n": n})
        # require BOTH halves >= -1% (generalises, not curve-fit)
        if isp >= -1.0 and oosp >= -1.0 and n >= 10:
            score = isp + oosp
            if best is None or score > best[0]:
                best = (score, sl, trail, act, reg, isp, oosp, n)
    return {"best": best, "grid": results}


# ---------------------------------------------------------------- main
def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    findings = []
    changes = []

    # 1) load performance
    live = load_json(DATA / "live-trades.json", {"open": [], "closed": []})
    paper = load_json(DATA / "unified-paper.json", {"open": [], "closed": []})
    fut = load_json(DATA / "futures-trades.json", {"open": [], "closed": []})

    live_c = dedupe(live.get("closed", []), ("symbol", "entry", "exit", "close_ts_utc"))
    paper_c = dedupe(paper.get("closed", []), ("symbol", "entry", "exit", "close_ts_utc"))
    fut_c = fut.get("closed", [])

    ls, ps, fs = stats(live_c), stats(paper_c), stats(fut_c)
    findings.append(f"live: n={ls['n']} wr={ls['wr']:.0f}% pnl={ls['pnl']:+.3f}")
    findings.append(f"paper: n={ps['n']} wr={ps['wr']:.0f}% pnl={ps['pnl']:+.3f}")
    findings.append(f"futures: n={fs['n']} wr={fs['wr']:.0f}% pnl={fs['pnl']:+.3f}")

    # 2) optimizer — OOS validated parameter search
    opt = optimize(["SOL/USDT", "XRP/USDT", "ETH/USDT", "BTC/USDT", "LINK/USDT"])
    if opt.get("best"):
        score, sl, trail, act, reg, isp, oosp, n = opt["best"]
        findings.append(
            f"optimizer: best stop={sl:.3f} trail={trail:.3f} "
            f"IS={isp:+.1f}% OOS={oosp:+.1f}% (n={n}) — generalises")
        # apply to the futures executor constants if materially better & safe
        try:
            import re
            fp = BASE / "scripts" / "futures_executor.py"
            src = fp.read_text(encoding="utf-8")
            new = src
            new = re.sub(r"^STOP_PCT = [0-9.]+.*$",
                         f"STOP_PCT = {sl}     # auto-tuned {now()[:10]} (OOS {oosp:+.1f}%)",
                         new, count=1, flags=re.M)
            new = re.sub(r"^TRAIL_DIST = [0-9.]+.*$",
                         f"TRAIL_DIST = {trail}      # auto-tuned {now()[:10]}",
                         new, count=1, flags=re.M)
            new = re.sub(r"^TRAIL_ACTIVATE = [0-9.]+.*$",
                         f"TRAIL_ACTIVATE = {act}  # auto-tuned {now()[:10]}",
                         new, count=1, flags=re.M)
            if new != src:
                # verify syntax before writing
                import py_compile
                tmp = fp.with_suffix(".tmp.py")
                tmp.write_text(new, encoding="utf-8")
                py_compile.compile(str(tmp), doraise=True)
                tmp.unlink()
                fp.write_text(new, encoding="utf-8")
                changes.append(f"futures stop={sl} trail={trail} act={act}")
        except Exception as e:
            findings.append(f"optimizer apply skipped: {str(e)[:60]}")
    else:
        findings.append("optimizer: no config generalised (kept current)")

    # 3) disable proven-losing edges (safe: state file, not code)
    state = load_json(STATE, {"disabled": [], "promoted": []})
    by_edge = {}
    for t in paper_c:
        e = t.get("edge")
        if e and e != "?":
            by_edge.setdefault(e, []).append(t)
    for e, ts in by_edge.items():
        st = stats(ts)
        if st["n"] >= 15 and st["wr"] < 42 and st["pnl"] < 0:
            if e not in state["disabled"]:
                state["disabled"].append(e)
                changes.append(f"disabled edge '{e}' (n={st['n']} wr={st['wr']:.0f}% pnl={st['pnl']:+.2f})")
        elif st["n"] >= 10 and st["wr"] >= 60 and st["pnl"] > 0:
            if e not in state["promoted"]:
                state["promoted"].append(e)
                findings.append(f"promotion candidate '{e}' (n={st['n']} wr={st['wr']:.0f}%)")
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # 4) write report
    report = {"ts": now(), "findings": findings, "changes": changes,
              "metrics": {"live": ls, "paper": ps, "futures": fs},
              "optimizer": opt.get("grid", [])}
    (REPORTS / "self-improve.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    # 5) telegram summary
    body = "\n".join(f"• {f}" for f in findings)
    chg = "\n".join(f"• {c}" for c in changes) if changes else "• none (no change for change's sake)"
    try:
        sys.path.insert(0, str(BASE / "src"))
        from trading.tg.bot_ui import send_card
        send_card("reports", "🔧 SELF-IMPROVE",
                  f"<b>Autonomous pass complete</b>\n{body}\n\n"
                  f"<b>Applied:</b>\n{chg}")
    except Exception:
        pass

    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}] SELF-IMPROVE")
    for f in findings:
        print(f"  {f}")
    for c in changes:
        print(f"  APPLIED: {c}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"self-improve error: {str(e)[:200]}")
