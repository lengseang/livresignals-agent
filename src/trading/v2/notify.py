"""Telegram notifications for V2, through the box's existing `hermes send` CLI (the same path the VPS heartbeat uses).

Rules (each tested):
  * it NEVER raises and never blocks for long: a notification failure must not break a trade (repo hard rule);
  * rate-limited per event key (state in a small JSON file), so a flapping condition cannot spam;
  * anything that looks like a credential is redacted from the text before it leaves the process;
  * the sender is injectable; tests never spawn a process.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("trading.v2.notify")
SECRET = re.compile(r"(apikey_[A-Za-z0-9_]+|Bearer\s+\S+|[A-Za-z0-9]{40,})")
TIMEOUT_S = 15.0


def redact(text: str) -> str:
    return SECRET.sub("[redacted]", text)


def hermes_sender(subject: str, body: str) -> bool:
    import os
    exe = os.environ.get("HERMES_BIN", "").strip() or shutil.which("hermes")   # pm2 may not have hermes on PATH
    if exe is None:
        log.warning("notify: hermes CLI not found; message dropped: %s", subject)
        return False
    r = subprocess.run([exe, "send", "--to", "telegram", "--subject", subject, body],
                       capture_output=True, timeout=TIMEOUT_S, check=False)
    return r.returncode == 0


class Notifier:
    def __init__(self, state_path: Optional[Path] = None, sender: Callable[[str, str], bool] = hermes_sender,
                 clock: Callable[[], float] = time.time):
        self.state_path, self.sender, self.clock = state_path, sender, clock

    def _state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text()) if self.state_path and self.state_path.exists() else {}
        except Exception:
            return {}

    def send(self, subject: str, body: str, *, key: Optional[str] = None, min_interval_s: float = 0.0) -> bool:
        """True iff a message was handed to the sender successfully. Never raises."""
        try:
            now = self.clock()
            st = self._state()
            if key and min_interval_s > 0 and now - float(st.get(key, 0)) < min_interval_s:
                return False
            ok = bool(self.sender(redact(subject)[:120], redact(body)[:3500]))
            if ok and key and self.state_path:
                st[key] = now
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps(st))
            return ok
        except Exception as exc:          # never let a notification break anything
            log.warning("notify failed: %s", type(exc).__name__)
            return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="send one V2 notification (never fails the caller)")
    ap.add_argument("subject")
    ap.add_argument("body")
    a = ap.parse_args(argv)
    Notifier().send("[V2 paper] " + a.subject, a.body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
