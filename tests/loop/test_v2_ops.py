"""V2-16 operations: notifier never breaks anything, preflight blocks bad starts, watchdog alerts on transitions."""
import json
import os

import pytest

from trading.v2.notify import Notifier, redact
from trading.v2 import preflight
from trading.v2.watchdog import run as watch


class Sink:
    def __init__(self, ok=True, boom=False):
        self.msgs, self.ok, self.boom = [], ok, boom

    def __call__(self, subject, body):
        if self.boom:
            raise OSError("no hermes")
        self.msgs.append((subject, body))
        return self.ok


# ------------------------------------------------------------ notifier

def test_notifier_never_raises():
    assert Notifier(sender=Sink(boom=True)).send("s", "b") is False


def test_notifier_redacts_secrets():
    s = Sink()
    Notifier(sender=s).send("x", "key apikey_FAKE0000_notarealkey000000 and Bearer abc.def")
    assert "apikey_" not in s.msgs[0][1] and "abc.def" not in s.msgs[0][1]
    assert redact("a" * 64) == "[redacted]"


def test_notifier_rate_limits_per_key(tmp_path):
    s, t = Sink(), [1000.0]
    n = Notifier(tmp_path / "st.json", sender=s, clock=lambda: t[0])
    assert n.send("a", "b", key="k", min_interval_s=600)
    assert not n.send("a", "b", key="k", min_interval_s=600)
    t[0] += 601
    assert n.send("a", "b", key="k", min_interval_s=600) and len(s.msgs) == 2


# ------------------------------------------------------------ preflight

def ok_get(url):
    if "binance" in url:
        import time
        return 200, json.dumps({"serverTime": int(time.time() * 1000)}).encode()
    return 404, b""


def test_preflight_passes_with_key_and_network(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    assert preflight.main(["--out", str(tmp_path)], http_get=ok_get) == 0
    rep = json.loads((tmp_path / "preflight.json").read_text())
    assert rep["ok"] and {c["name"] for c in rep["checks"]} >= {"workflows", "paper_only", "chains_verify",
                                                                  "binance_data_api", "jev_key_present"}
    assert "x" not in json.dumps([c["detail"] for c in rep["checks"] if c["name"] == "jev_key_present"]).replace("MISSING", "")


def test_preflight_fails_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert preflight.main(["--out", str(tmp_path), "--offline"]) == 1


def test_preflight_fails_on_a_broken_chain(tmp_path, monkeypatch):
    from trading.shadow.chain import ShadowChain
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    ch = ShadowChain(tmp_path, "v2paper", "book", 1)
    ch.write({"ts_event_ms": 1_760_000_000_000, "v": 1})
    f = next((tmp_path / "v2paper" / "book").glob("*.jsonl"))
    f.write_text(f.read_text().replace('"v":1', '"v":2'))
    assert preflight.main(["--out", str(tmp_path), "--offline"]) == 1


def test_preflight_fails_on_clock_skew_or_unreachable_binance(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    skew = lambda u: (200, json.dumps({"serverTime": 1}).encode()) if "binance" in u else (404, b"")  # noqa: E731
    assert preflight.main(["--out", str(tmp_path)], http_get=skew) == 1
    down = lambda u: (451, b"")  # noqa: E731
    assert preflight.main(["--out", str(tmp_path)], http_get=down) == 1


# ------------------------------------------------------------ watchdog

def hb(tmp_path, **kw):
    d = {"jev_calls": 0, "jev_ok": 0, "feed_failures": 0, "kill_engaged": False, "kill_reasons": [], "outcomes": {}}
    d.update(kw)
    (tmp_path / "heartbeat.json").write_text(json.dumps(d))


def test_watchdog_alerts_once_per_transition_and_on_recovery(tmp_path):
    s = Sink()
    n = Notifier(sender=s)
    hb(tmp_path, kill_engaged=True, kill_reasons=["daily loss"])
    r1 = watch(tmp_path, n)
    assert "kill_engaged" in r1["problems"] and r1["sent"] == ["kill_engaged"]
    assert watch(tmp_path, n)["sent"] == []                        # still engaged: no repeat
    hb(tmp_path)
    assert watch(tmp_path, n)["sent"] == ["recovered:kill_engaged"]


def test_watchdog_silent_runner_and_jev_failing(tmp_path):
    import time
    hb(tmp_path, jev_calls=20, jev_ok=3)
    old = time.time() - 900
    os.utime(tmp_path / "heartbeat.json", (old, old))
    p = watch(tmp_path, Notifier(sender=Sink()))["problems"]
    assert "runner_silent" in p and "jev_failing" in p


def test_watchdog_never_raises_on_garbage_and_sends_digest(tmp_path):
    (tmp_path / "heartbeat.json").write_text("{not json")
    s = Sink()
    r = watch(tmp_path, Notifier(sender=s), daily=True)
    assert "runner_silent" in r["problems"] and "digest" in r["sent"]


def test_watchdog_cycle_errors_and_feed_growth(tmp_path):
    n = Notifier(sender=Sink())
    hb(tmp_path, feed_failures=1, outcomes={"cycle_error": 0})
    watch(tmp_path, n)
    hb(tmp_path, feed_failures=9, outcomes={"cycle_error": 2})
    p = watch(tmp_path, n)["problems"]
    assert "feed_failing" in p and "cycle_errors" in p


def test_watchdog_retries_an_alert_that_was_not_delivered(tmp_path):
    hb(tmp_path, kill_engaged=True)
    down = Sink(ok=False)
    r1 = watch(tmp_path, Notifier(sender=down))
    assert r1["sent"] == [] and r1["undelivered"] == ["kill_engaged"]
    up = Sink()
    r2 = watch(tmp_path, Notifier(sender=up))
    assert r2["sent"] == ["kill_engaged"] and up.msgs and r2["undelivered"] == []
    assert watch(tmp_path, Notifier(sender=up))["sent"] == []


def test_failed_digest_is_not_reported_as_sent(tmp_path):
    hb(tmp_path)
    assert "digest" not in watch(tmp_path, Notifier(sender=Sink(ok=False)), daily=True)["sent"]
