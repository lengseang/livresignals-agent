"""Gold (XAUUSD/GC=F) signal scanner with RESULT TRACKING.

Every BUY-DIP signal gets recorded with its levels (entry / SL = dip low /
TP = 2R). On later scans, if price hits TP first -> ping WIN; hits SL first
-> ping LOSS; 12h passes unresolved -> ping TIMEOUT (no clear result).

This gives the owner a real accuracy record: 'signal said buy, did it reach
the success point?' — exactly the honesty loop requested 2026-09-09.

Data: Yahoo GC=F 15m (gold futures proxy for XAUUSD).
"""
import json
import sys
import urllib.request
import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_PATH = BASE / "data" / "gold-signals.json"
SYM = "GC=F"

# validation window + levels
VALID_HOURS = 12.0
# 🔴 TRACKED-HOURS fixes a censoring defect found 2026-09-22.
#
# The old logic scored a signal TIMEOUT at VALID_HOURS and then STOPPED LOOKING.
# That is censoring, and it biases the record in the flattering direction:
#
#   * a TIMEOUT means price hit NEITHER the stop nor the target inside 12h
#   * at R:R 2.00 the target is far, so a timeout is disproportionately likely
#     to be a trade that is drifting AGAINST us, not yet far enough to stop out
#   * once abandoned, a timeout that later reaches its stop is never recorded
#
# Measured on the 11 resolved signals: 4 were TIMEOUTS, and 2 of those 4 were
# already LOSING at the moment they were abandoned (-0.016%, -0.630%). Their
# eventual outcome was simply never checked, so the tail is invisible.
#
# So a timeout is no longer terminal. It becomes TRACKING, and the walk continues
# until the stop or target is genuinely reached, or TRACKING_MAX_HOURS expires.
# The signal is only reported as a soft timeout once, so the owner still hears
# about it promptly; the record keeps looking afterwards.
TRACKING_MAX_HOURS = 72.0
TP_MULT = 2.0       # TP = entry + 2x SL distance (matches EA InpTPRR)
SL_BUFFER = 0.001   # 0.1% below dip low


def fetch_15m(n=300):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{SYM}"
           f"?interval=15m&range=3d")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())
    res = d["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        h = q["high"][i]
        lo = q["low"][i]
        if c is None:
            continue
        bars.append({"t": t, "c": float(c), "h": float(h or c), "l": float(lo or c)})
    return bars


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(-n, 0):
        dx = closes[i] - closes[i - 1]
        if dx > 0:
            gains += dx
        else:
            losses -= dx
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + (gains / n) / (losses / n))


def load_state() -> list:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_state(sigs: list) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(sigs, indent=2), encoding="utf-8")


def send_card(title: str, body: str) -> None:
    sys.path.insert(0, str(BASE.parent / "src"))
    from trading.tg.bot_ui import send_card
    try:
        send_card("gold", title, body)     # gold lives in its own topic now
    except Exception:
        pass


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def scan() -> dict | None:
    """Return current market state dict, or None if no data."""
    bars = fetch_15m()
    if len(bars) < 60:
        return None
    closes = [b["c"] for b in bars]
    px = closes[-1]
    hi = max(b["h"] for b in bars[-90:])
    lo = min(b["l"] for b in bars[-90:])
    dd = px / hi - 1
    r = rsi(closes)
    r_prev = rsi(closes[:-1])
    turning = r > r_prev
    chg1h = (px / closes[-5] - 1) * 100
    return {
        "ts": now_utc().isoformat(), "px": px, "hi": hi, "lo": lo,
        "dd": dd, "rsi": r, "turning": turning, "chg1h": chg1h,
    }


def check_open_signals(sigs: list, st: dict) -> list:
    """Resolve any open signals against current price. Returns event strings."""
    events = []
    still_open = []
    now = now_utc()
    for s in sigs:
        # TRACKING signals are still live: a soft timeout that has not yet
        # reached its stop or target. Excluding them would re-introduce the
        # censoring this change removes.
        if s.get("status") not in ("OPEN", "TRACKING"):
            still_open.append(s)
            continue
        # get latest bars to test high/low since signal time
        try:
            bars = fetch_15m()
            fire_epoch = datetime.datetime.fromisoformat(s["fire_ts"]).timestamp()
            since = [b for b in bars if b["t"] >= fire_epoch - 60]
            if not since:
                still_open.append(s)
                continue
            px_now = bars[-1]["c"]
        except Exception:
            still_open.append(s)
            continue
        age_h = (now - datetime.datetime.fromisoformat(s["fire_ts"])).total_seconds() / 3600
        # FIRST-TOUCH resolution (bug fix 2026-09-15). The old code compared the
        # window's max high against TP BEFORE checking the min low against SL, so
        # any signal that EVER grazed TP was scored a WIN even when the SL was
        # broken first -> the owner's accuracy record was optimistically biased.
        # Walk the bars in chronological order and stop at the first level hit.
        result = None
        for b in since:
            if b["l"] <= s["sl"]:
                result = "LOSS"
                break
            if b["h"] >= s["tp"]:
                result = "WIN"
                break
        # ── censoring fix (2026-09-22) ────────────────────────────────────────
        # A signal that reached neither level is NOT final at VALID_HOURS. The
        # old code wrote TIMEOUT and stopped, which discarded the subsequent
        # outcome entirely — and because a 2:1 target is far, timeouts skew
        # toward trades that are quietly losing rather than ones that are flat.
        #
        # Now: at VALID_HOURS the signal is flagged for the first time
        # (so the owner is told promptly, as before), but it stays TRACKING and
        # the walk above continues on every later run. Only a genuine stop or
        # target ends it. If nothing resolves it by TRACKING_MAX_HOURS the
        # position is closed and marked TIMEOUT-EXPIRED at the market price,
        # with that price recorded so the outcome is visible rather than absent.
        if result is None:
            if age_h >= TRACKING_MAX_HOURS:
                # genuinely undecided even after the extended window
                result = "TIMEOUT-EXPIRED"
            elif age_h >= VALID_HOURS and s.get("status") == "OPEN":
                # first notification only — the record keeps tracking
                s["timed_out_at"] = now.isoformat()
                s["status"] = "TRACKING"
                still_open.append(s)
                events.append("result:soft-timeout")
                try:
                    send_card("🥇 GOLD SIGNAL ⏸ STILL OPEN",
                              f"<b>No resolution in {VALID_HOURS:.0f}h — still "
                              f"tracking.</b>\n"
                              f"Entry ${s['fire_px']:,.1f}  ·  "
                              f"now ${px_now:,.1f}  ·  "
                              f"({(px_now/s['fire_px']-1)*100:+.2f}%)\n"
                              f"Still watching for the stop or target, up to "
                              f"{TRACKING_MAX_HOURS:.0f}h total.")
                except Exception:  # noqa: BLE001
                    pass
                continue
            else:
                still_open.append(s)
                continue
        if result:
            s["status"] = result
            s["resolve_ts"] = now.isoformat()
            s["px_now"] = px_now
            # For a soft timeout that was later resolved, record how long the
            # resolution actually took. Without this the extended tracking is
            # invisible and nobody can see whether the 12h window was the
            # problem or the signal was.
            if s.get("timed_out_at"):
                try:
                    t0 = datetime.datetime.fromisoformat(s["timed_out_at"])
                    s["tracked_extra_h"] = round(
                        (now - t0).total_seconds() / 3600, 2)
                except Exception:  # noqa: BLE001
                    pass
            # RETAIN the resolved signal (bug fix 2026-09-15). It used to be
            # dropped from the list, so state was rewritten without it and the
            # WIN/LOSS counters below read a list that could never contain a
            # resolved signal -> "accuracy=no results yet" forever, despite 11
            # real results already logged in logs/gold.log. The owner's honesty
            # loop needs the accumulated record, not a per-run snapshot.
            still_open.append(s)
            events.append(f"result:{result}")
            # RUNNING RECORD in the ping (2026-09-15): the resolved signals are
            # retained above, so the tally can finally be shown where the owner
            # actually looks. Before the fix the ping carried no running record
            # and the log printed "no results yet" forever.
            _w = sum(1 for x in still_open if x.get("status") == "WIN")
            _l = sum(1 for x in still_open if x.get("status") == "LOSS")
            tally = (f"\n\n📊 <b>Gold dip-signal record: {_w}W / {_l}L"
                     + (f" ({_w / (_w + _l) * 100:.0f}% hit rate)</b>"
                        if _w + _l else "</b>"))
            if result == "WIN":
                send_card("🥇 GOLD SIGNAL ✅ WIN",
                          f"<b>Signal HIT its target!</b>\n"
                          f"Fired: ${s['fire_px']:.0f} ({s['fire_ts'][:16]} UTC)\n"
                          f"TP ${s['tp']:.0f} reached — price now ${px_now:.0f}\n"
                          f"RSI was {s['fire_rsi']:.0f}, dip {s['fire_dd']*100:.2f}%\n"
                          f"<b>Accuracy: signal called the dip bottom ✅</b>" + tally)
            elif result == "LOSS":
                send_card("🥇 GOLD SIGNAL ❌ LOSS",
                          f"<b>Signal FAILED its level.</b>\n"
                          f"Fired: ${s['fire_px']:.0f} ({s['fire_ts'][:16]} UTC)\n"
                          f"SL ${s['sl']:.0f} broken — price now ${px_now:.0f}\n"
                          f"RSI was {s['fire_rsi']:.0f}, dip {s['fire_dd']*100:.2f}%\n"
                          f"<b>Accuracy: dip kept falling — signal wrong ❌</b>" + tally)
            else:
                # TIMEOUT-EXPIRED: the only surviving way to be undecided.
                # TRACKING_MAX_HOURS elapsed without the stop or the target being
                # reached. Scored NEUTRAL with its closing price recorded, so it
                # contributes a visible outcome instead of vanishing.
                #
                # Deliberately NOT a loss. Nothing was proven wrong — price never
                # travelled far enough in either direction. Calling that a
                # failure would overstate the error rate exactly as the old
                # 12h censoring understated it.
                pnl_pct = (px_now / s["fire_px"] - 1) * 100
                send_card("🥇 GOLD SIGNAL ⏹ EXPIRED",
                          f"<b>Closed after {TRACKING_MAX_HOURS:.0f}h with no "
                          f"resolution.</b>\n"
                          f"Fired: ${s['fire_px']:.0f} ({s['fire_ts'][:16]} UTC)\n"
                          f"Neither TP ${s['tp']:.0f} nor SL ${s['sl']:.0f} hit.\n"
                          f"Closed at ${px_now:.0f} ({pnl_pct:+.2f}%)\n"
                          f"<b>Accuracy: neutral — no move either way.</b>" + tally)
        else:
            still_open.append(s)
    return still_open, events


def main() -> None:
    st = scan()
    if not st:
        print("gold scan: no data")
        return

    sigs = load_state()
    # 1) resolve open signals first
    resolved, events = check_open_signals(sigs, st)
    # 2) new signal?
    fired = False
    if st["dd"] <= -0.0025 and st["rsi"] <= 32 and st["turning"]:
        # avoid duplicate: don't fire if an OPEN signal exists from last 6h
        dup = any(s.get("status") in ("OPEN", "TRACKING") and
                  (now_utc() - datetime.datetime.fromisoformat(s["fire_ts"])).total_seconds() < 21600
                  for s in resolved)
        if not dup:
            entry = st["px"]
            sl = st["lo"] * (1 - SL_BUFFER)
            tp = entry + (entry - sl) * TP_MULT
            sig = {
                "fire_ts": now_utc().isoformat(),
                "fire_px": round(entry, 1),
                "fire_rsi": round(st["rsi"], 1),
                "fire_dd": round(st["dd"], 5),
                "hi": st["hi"], "lo": st["lo"],
                "sl": round(sl, 1), "tp": round(tp, 1),
                "status": "OPEN",
            }
            resolved.append(sig)
            fired = True
            save_state(resolved)
            send_card("🥇 GOLD BUY-DIP SETUP",
                      f"<b>Signal fired @ ${entry:.0f}</b>\n"
                      f"RSI(14): {st['rsi']:.0f} | dip: {st['dd']*100:.2f}%\n"
                      f"1h: {st['chg1h']:+.2f}%\n\n"
                      f"Levels (2R plan):\n"
                      f"🎯 TP: ${tp:.0f}\n"
                      f"🛑 SL: ${sl:.0f} (below dip low)\n\n"
                      f"⏱️ I'll track this and ping the RESULT "
                      f"(win/loss/timeout) automatically.")
        else:
            print("gold: signal condition met but already have an open signal")
    else:
        save_state(resolved)

    # 3) print status line
    n_open = sum(1 for s in resolved
                 if s.get("status") in ("OPEN", "TRACKING"))
    wins = sum(1 for s in resolved if s.get("status") == "WIN")
    losses = sum(1 for s in resolved if s.get("status") == "LOSS")
    total_r = wins + losses
    acc = f"{wins}/{total_r} ({wins/total_r*100:.0f}%)" if total_r else "no results yet"
    print(f"GOLD px=${st['px']:.0f} rsi={st['rsi']:.0f} dd={st['dd']*100:.2f}% "
          f"open={n_open} resolved={total_r} accuracy={acc}")
    for e in events:
        print(f"  gold: {e}")


if __name__ == "__main__":
    main()
