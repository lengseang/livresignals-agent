#!/usr/bin/env python3
"""ECONOMIC EVENT RISK — know WHEN volatility is coming, before it lands.

Owner's point: we must account for events (elections, news, data releases) that
drive the market, not just technicals.

We cannot predict the OUTCOME of an event -- that's priced in within seconds.
We CAN know the CALENDAR: exactly when high-impact releases land. Around those
windows, spread widens, slippage spikes and stops get hunted, so the correct
play is to reduce risk rather than guess direction.

Source: ForexFactory weekly calendar JSON (free, includes impact level + time).
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
CACHE = BASE / "data" / "econ-calendar.json"
SEED = BASE / "data" / "ff-seed.json"     # last known-good calendar
FEEDS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]
CACHE_HOURS = 6

# events that historically move gold the most
GOLD_MOVERS = ("federal funds rate", "fomc", "non-farm", "nonfarm", "cpi",
               "consumer price", "ppi", "producer price", "pce", "gdp",
               "unemployment", "retail sales", "powell", "interest rate")


def _fetch(url):
    """Fetch one feed. Returns [] on any failure (the host bot-challenges)."""
    try:
        raw = subprocess.run(
            ["curl", "-s", "-L", "--http1.1", "--max-time", "25",
             "-A", "Mozilla/5.0", url],
            capture_output=True, text=True, timeout=40).stdout
        if not raw.strip().startswith("["):
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_seed(events):
    """Merge fetched events into the last-known-good seed."""
    try:
        merged = {}
        if SEED.exists():
            for e in json.loads(SEED.read_text()):
                merged[(e.get("title"), e.get("date"))] = e
        for e in events:
            merged[(e.get("title"), e.get("date"))] = e
        SEED.write_text(json.dumps(list(merged.values()), indent=1))
    except Exception:
        pass


def _load_seed():
    if not SEED.exists():
        return []
    try:
        return json.loads(SEED.read_text())
    except Exception:
        return []


def fetch_events(force=False):
    if not force and CACHE.exists():
        try:
            c = json.loads(CACHE.read_text())
            age = (datetime.now(timezone.utc).timestamp() - c["ts"]) / 3600
            if age < CACHE_HOURS and c.get("events"):
                return c["events"]
        except Exception:
            pass
    events = []
    for u in FEEDS:
        events.extend(_fetch(u))
    if events:
        _save_seed(events)
    else:
        events = _load_seed()          # network blocked -> use last known good
    norm = []
    for e in events:
        try:
            dt = datetime.fromisoformat(e["date"])
            norm.append({"title": e.get("title", ""),
                         "country": e.get("country", ""),
                         "impact": e.get("impact", ""),
                         "ts": dt.timestamp(),
                         "iso": dt.isoformat(),
                         "date": dt})
        except Exception:
            continue
    norm.sort(key=lambda x: x["ts"])
    if norm:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        clean = [{k: v for k, v in e.items() if k != "date"} for e in norm]
        CACHE.write_text(json.dumps(
            {"ts": datetime.now(timezone.utc).timestamp(), "events": clean}, indent=1))
        for e in norm:
            e["date"] = datetime.fromtimestamp(e["ts"], timezone.utc)
    return norm


def is_gold_mover(title):
    t = title.lower()
    return any(k in t for k in GOLD_MOVERS)


def upcoming(events=None, hours=72, only_high=True, usd_only=True):
    events = events if events is not None else fetch_events()
    now = datetime.now(timezone.utc).timestamp()
    out = []
    for e in events:
        if e["ts"] < now or e["ts"] > now + hours * 3600:
            continue
        if only_high and e.get("impact") != "High":
            continue
        if usd_only and e.get("country") not in ("USD", "US", "All"):
            continue
        out.append(e)
    return out


def risk_state(hours_before=24, hours_after=6, events=None):
    """Are we inside a blackout window around a high-impact USD event?"""
    events = events if events is not None else fetch_events()
    now = datetime.now(timezone.utc).timestamp()
    for e in events:
        if e.get("impact") != "High" or e.get("country") not in ("USD", "US"):
            continue
        if now - hours_before * 3600 <= e["ts"] <= now + hours_after * 3600:
            mins = (e["ts"] - now) / 60
            return {"blackout": True, "event": e["title"], "iso": e["iso"],
                    "minutes_until": round(mins),
                    "gold_mover": is_gold_mover(e["title"])}
    nxt = upcoming(events, hours=hours_before)
    return {"blackout": False,
            "next": nxt[0]["title"] if nxt else None,
            "next_iso": nxt[0]["iso"] if nxt else None}


if __name__ == "__main__":
    ev = fetch_events(force=True)
    now = datetime.now(timezone.utc)
    print(f"=== ECONOMIC CALENDAR ({len(ev)} events loaded) ===")
    print(f"now: {now:%Y-%m-%d %H:%M} UTC\n")
    up = upcoming(ev, hours=168)
    print(f"High-impact USD events in the next 7 days: {len(up)}")
    for e in up:
        gm = " ⭐GOLD" if is_gold_mover(e["title"]) else ""
        hrs = (e["date"] - now).total_seconds() / 3600
        print(f"  {e['iso'][:16]}Z | in {hrs:6.1f}h | {e['title']}{gm}")
    st = risk_state(events=ev)
    print("\n=== RISK STATE ===")
    if st["blackout"]:
        print(f"  🔴 BLACKOUT — {st['event']} in {st['minutes_until']} min "
              f"(gold mover: {st['gold_mover']})")
        print("  -> no new entries; spread/slippage spike around the release")
    else:
        print("  🟢 clear — not inside an event window")
    if up:
        nxt = up[0]
        hrs = (nxt["date"] - now).total_seconds() / 3600
        print(f"  next event: {nxt['title']} in {hrs:.1f}h")
        if hrs <= 48:
            print(f"  ⚠️  within 48h — blackout starts at T-24h "
                  f"({(hrs-24):.1f}h from now)")
    else:
        print("  no high-impact USD events in the next 7 days")
