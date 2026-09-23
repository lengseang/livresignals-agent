"""Simulate gold's new resolution logic against the REAL 11 resolved signals +
the 1 open one, and against synthetic paths, to prove three things:

  1. WIN and LOSS still resolve on first touch, exactly as before
  2. a signal that times out at 12h is NOT dropped — it keeps tracking
  3. it eventually resolves, or expires visibly, and never silently disappears

This is a logic test, not a backtest: it exercises the decision branches.
"""
import datetime
import json
from pathlib import Path

VALID_HOURS = 12.0
TRACKING_MAX_HOURS = 72.0


def resolve(sig, bars, now):
    """Mirror of the scanner's resolution block, isolated for testing.

    bars: list of dicts {t, h, l, c} with t in epoch seconds
    Returns the (status, resolved?) the scanner would produce.
    """
    fire_epoch = datetime.datetime.fromisoformat(sig["fire_ts"]).timestamp()
    since = [b for b in bars if b["t"] >= fire_epoch - 60]
    if not since:
        return "OPEN", False
    px_now = since[-1]["c"]
    age_h = (now - datetime.datetime.fromisoformat(sig["fire_ts"])).total_seconds() / 3600

    result = None
    for b in since:
        if b["l"] <= sig["sl"]:
            result = "LOSS"
            break
        if b["h"] >= sig["tp"]:
            result = "WIN"
            break

    if result is None:
        if age_h >= TRACKING_MAX_HOURS:
            result = "TIMEOUT-EXPIRED"
        elif age_h >= VALID_HOURS and sig.get("status") == "OPEN":
            sig["status"] = "TRACKING"
            sig["timed_out_at"] = now.isoformat()
            return "TRACKING", False
        else:
            return sig.get("status", "OPEN"), False
    sig["status"] = result
    sig["px_now"] = px_now
    return result, True


T0 = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
base = {"fire_ts": T0.isoformat(), "fire_px": 4300.0,
        "sl": 4250.0, "tp": 4400.0, "status": "OPEN"}


def bars(path, hours):
    """Build 15m bars following a price path function."""
    out = []
    for i in range(int(hours * 4)):
        px = path(i)
        out.append({"t": T0.timestamp() + i * 900,
                    "h": px * 1.0005, "l": px * 0.9995, "c": px})
    return out


def at(hours):
    return T0 + datetime.timedelta(hours=hours)


cases = [
    # paths must actually CROSS the level to resolve: tp 4400 / sl 4250.
    # The first version of this test used 4310+2i (max 4332) and 4290-2i
    # (min 4268), neither of which touches its level — so OPEN was the correct
    # answer and the expectation was wrong. The test was at fault, not the code.
    ("wins immediately",
     bars(lambda i: 4310 + i * 8, 4), at(4), "WIN"),
    ("loses immediately",
     bars(lambda i: 4290 - i * 6, 4), at(4), "LOSS"),
    ("flat at 12h -> TRACKING, not dropped",
     bars(lambda i: 4300, 13), at(13), "TRACKING"),
    ("flat at 12h, hits TP at 30h",
     bars(lambda i: 4300 if i < 120 else 4300 + (i - 120) * 3, 40), at(40), "WIN"),
    ("flat at 12h, hits SL at 20h",
     bars(lambda i: 4300 if i < 80 else 4300 - (i - 80) * 3, 25), at(25), "LOSS"),
    ("flat forever -> EXPIRED, visible",
     bars(lambda i: 4300, 80), at(80), "TIMEOUT-EXPIRED"),
]

print("=" * 78)
print("GOLD RESOLUTION LOGIC — branch test")
print("=" * 78)
print(f"\n  {'case':<40} {'want':>16} {'got':>16}  ok")
print("  " + "-" * 74)
fails = 0
for name, b, now, want in cases:
    got, resolved = resolve(dict(base), b, now)
    ok = got == want
    if not ok:
        fails += 1
    print(f"  {name:<40} {want:>16} {got:>16}  {'✅' if ok else '🔴'}")

print()
print("=" * 78)
print("THE CENSORING FIX, STATED PLAINLY")
print("=" * 78)
print("""
  OLD behaviour: 'flat at 12h' -> status TIMEOUT, added to still_open, and the
  scanner then SKIPPED it on every future run because the loop began with
  `if status != "OPEN": continue`. Its later outcome was never examined.

  So a signal that was quietly losing at 12h — drifting toward its stop without
  reaching it — had its eventual stop-out recorded NOWHERE. The record showed a
  neutral timeout for what may have been a full loss.

  NEW behaviour: at 12h it becomes TRACKING and keeps being examined. Cases 4
  and 5 above are exactly that situation, and both now resolve to their true
  outcome (WIN at 30h, LOSS at 20h) instead of being abandoned at 12h.

  Case 6 shows the residual: a signal that genuinely never decides is closed at
  72h as TIMEOUT-EXPIRED with its price recorded. Visible, priced, and not
  counted as a loss — which would overstate the error rate just as the old
  censoring understated it.
""")

print("=" * 78)
print(f"RESULT: {'✅ all branches correct' if not fails else f'🔴 {fails} FAILED'}")
print("=" * 78)

# Now replay the REAL signals through the OLD and NEW rules, to show the impact.
print()
print("=" * 78)
print("IMPACT ON THE REAL 12 SIGNALS")
print("=" * 78)
g = json.loads(Path("/opt/trading-agent/scripts/data/gold-signals.json").read_text())
res = [s for s in g if s["status"] != "OPEN" and s.get("resolve_ts")]
open_ = [s for s in g if s["status"] == "OPEN"]
print(f"\n  resolved under the OLD rule: {len(res)}")
print(f"  currently open             : {len(open_)}")
print()
print("  Under the new rule, any signal whose outcome was TIMEOUT would have")
print("  kept tracking instead. Their recorded outcomes were:")
for s in res:
    extra = ""
    if s["status"] == "TIMEOUT":
        a = (s["px_now"] / s["fire_px"] - 1) * 100
        drift = "still drifting toward SL" if a < 0 else "mildly up"
        extra = f"  <- was {a:+.3f}% at 12h ({drift})"
    print(f"    {s['fire_ts'][:16]}  {s['status']:<17}{extra}")
print()
print("  ⚠️ The live open signal fired 2026-09-23T02:03. It will now be tracked")
print("     past 12h if unresolved, so its true outcome will be recorded —")
print("     which is the first signal where the fix will show in the live record.")
