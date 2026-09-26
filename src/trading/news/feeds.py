"""Pinned public sources (docs/research/sources.md, T1.2) and a plain HTTP GET."""
from __future__ import annotations

import urllib.request

FEEDS = {
    "cointelegraph": "https://cointelegraph.com/rss",
    "theblock": "https://www.theblock.co/rss.xml",
    "decrypt": "https://decrypt.co/feed",
    # Answers 308 to .../rss (no slash); urllib's default redirect handler follows it.
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
}
FNG_URL = "https://api.alternative.me/fng/"

USER_AGENT = "livresignals-agent-news/0.1 (paper research; polls >= 5 min)"
TIMEOUT_S = 20
MAX_BYTES = 5 * 1024 * 1024


def fetch(url: str) -> bytes:
    """GET url and return the body. Raises on HTTP errors, timeouts and oversize bodies."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        body = resp.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"response larger than {MAX_BYTES} bytes")
    return body
