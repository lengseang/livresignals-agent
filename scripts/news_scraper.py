#!/usr/bin/env python3
"""News and market-intel scraper for the trading stack.

WHY THIS EXISTS
---------------
The strategy book answers "what does price do?" It cannot answer "why did it just
move?". A system that only sees candles is blind to the events that dominate
crypto returns: listings, delistings, hacks, regulation, ETF decisions.

This collects that context as a timestamped archive, so a later analysis can join
a price move to the news around it. It does NOT trade, generate signals, or feed
the live path. It builds a record.

WHY SCRAPLING
-------------
Plain HTTP clients get blocked by Cloudflare on most of these sources. Scrapling
impersonates browser TLS fingerprints, which is what makes this viable without an
API key per source.

WHAT WAS WRONG IN THE FIRST VERSION (2026-10-04)
------------------------------------------------
The first cut pulled page-wide text and kept lines of 35-160 characters. That
harvested marketing blurbs ("Unrivaled digital asset data...") and navigation
chrome, while missing every actual headline. It also logged a captcha challenge
page from Binance as a success (HTTP 202 with "verify that you're not a robot").

Three fixes:
  1. Extract from HEADING and ANCHOR elements, not page-wide text. Headlines live
     in <h1>-<h3> and in <a> tags; prose in between is usually promo copy.
  2. Detect challenge pages explicitly and record them as failures. A 202 that
     says "not a robot" is not a 202 that delivered content. Exit 0 is not
     success — the run must be judged on whether real headlines arrived.
  3. Per-source selectors, because these sites do not share a structure.

Dedupe by stable hash so re-running cannot append the same story repeatedly.
One source failing never aborts the run.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENT = Path("/opt/trading-agent")
OUT = AGENT / "data/news-archive.jsonl"
STATE = AGENT / "data/news-scraper-state.json"

TIMEOUT = 30
MAX_ITEMS = 30

# Signals that a page is a challenge/interstitial rather than content. If any
# appears in the extracted text, the source is recorded as BLOCKED, not OK.
CHALLENGE_MARKERS = (
    "not a robot", "enable javascript", "checking your browser",
    "just a moment", "attention required", "cf-chl", "verify you are human",
    "ddos protection", "please wait while we verify",
)

# Per-source extraction. `headline_selectors` are CSS selectors tried in order;
# the first that yields enough items wins. This is more robust than one global
# rule because the sites have completely different markup.
SOURCES = [
    {
        "name": "coindesk",
        "url": "https://www.coindesk.com/markets",
        "kind": "news",
        "selectors": ["h2 a::text", "h2::text", "h3 a::text", "article a::text"],
    },
    {
        "name": "cointelegraph",
        "url": "https://cointelegraph.com/",
        # Probed live 2026-10-04: this site moved its titles into bare <h3> text
        # (51 clean headlines) and a [class*=title] element (45). The old
        # h2-a / a[class*=title] / h2 guesses matched ~0-1, so the source
        # delivered almost nothing. h3::text first, then the class fallback.
        "kind": "news",
        "selectors": ["h3::text", "[class*=title]::text", "h2::text", "h2 a::text"],
    },
    {
        "name": "theblock",
        "url": "https://www.theblock.co/latest",
        # h3, not h2 — probed live 2026-10-04. The Block uses h3 for its article
        # titles; the original h2 guess matched nothing.
        "kind": "news",
        "selectors": ["h3 a::text", "h3::text", "h2 a::text"],
    },
]


def _load(p: Path, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(d) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(STATE)


def _hash(source: str, text: str) -> str:
    return hashlib.sha256(f"{source}|{text[:200]}".encode("utf-8")).hexdigest()[:20]


def _clean(s) -> str:
    if isinstance(s, list):
        s = " ".join(str(x) for x in s)
    return " ".join(str(s).split())


def _looks_like_headline(s: str) -> bool:
    if not (30 <= len(s) <= 200):
        return False
    low = s.lower()
    # marketing / boilerplate that is not news
    for bad in ("cookie", "subscribe", "sign up", "newsletter", "all rights",
                "terms of service", "privacy policy", "advertisement",
                "follow us", "download the app", "trusted by", "bespoke research",
                "unrivaled", "discover our", "learn more", "read more",
                "institutional standard", "leading provider"):
        if bad in low:
            return False
    # a headline usually contains a verb-ish or a market noun; require some
    # alphabetic word rather than a bare number or ticker soup
    words = [w for w in low.split() if len(w) > 3 and w.isalpha()]
    return len(words) >= 4


def _is_challenge(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in CHALLENGE_MARKERS)


def main() -> int:
    quiet = "--quiet" in sys.argv
    try:
        from scrapling.fetchers import Fetcher
    except Exception as e:
        print(f"scrapling unavailable: {e}")
        return 1

    state = _load(STATE, {"seen": [], "runs": 0})
    seen = set(state.get("seen", []))
    if len(seen) > 30000:
        seen = set(list(seen)[-30000:])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    new_items = 0
    results = {}
    blocked = []

    for src in SOURCES:
        name = src["name"]
        try:
            page = Fetcher.get(src["url"], timeout=TIMEOUT)
            whole = page.get_all_text()
            if _is_challenge(whole):
                results[name] = f"BLOCKED (challenge page, HTTP {page.status})"
                blocked.append(name)
                continue

            found = []
            for sel in src["selectors"]:
                try:
                    raw = page.css(sel)
                except Exception:
                    continue
                for item in raw:
                    s = _clean(item)
                    if _looks_like_headline(s):
                        found.append(s)
                if len(found) >= 5:
                    break        # this selector works; stop trying others

            got = 0
            with open(OUT, "a", encoding="utf-8") as f:
                for s in found[:MAX_ITEMS]:
                    h = _hash(name, s)
                    if h in seen:
                        continue
                    seen.add(h)
                    f.write(json.dumps({
                        "id": h, "source": name, "kind": src["kind"],
                        "url": src["url"], "text": s,
                        "fetched_utc": datetime.now(timezone.utc).isoformat(),
                    }) + "\n")
                    got += 1
                    new_items += 1
            # 🔴 Status must reflect EXTRACTION, not novelty.
            #
            # `got` counts only NEW (deduped) items. On any run after the first,
            # every headline is already in `seen`, so got==0 and the source was
            # reported as "NO HEADLINES — selectors need updating" EVEN THOUGH
            # extraction succeeded and merely found nothing new. Measured
            # 2026-10-04: all 3 sources read "NO HEADLINES (HTTP 200)" while the
            # archive held 23 correctly-extracted headlines from those very
            # sources — a false failure that sends maintenance chasing markup
            # that was never broken.
            #
            # `found` is the extraction count. If found>0 the selectors work.
            if found:
                results[name] = (f"ok HTTP {page.status} +{got} new "
                                 f"({len(found)} extracted)")
            else:
                results[name] = (f"NO HEADLINES (HTTP {page.status}) "
                                 f"— selectors need updating")
        except Exception as e:
            results[name] = f"FAIL {str(e)[:70]}"
        time.sleep(1.0)

    state["seen"] = list(seen)[-30000:]
    state["runs"] = state.get("runs", 0) + 1
    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    state["by_source"] = results
    _save(state)

    if not quiet:
        print(f"news scrape {datetime.now(timezone.utc).isoformat()[:19]}")
        for k, v in results.items():
            print(f"  {k:14} {v}")
        print(f"  new items: {new_items}")
        if blocked:
            print(f"  blocked sources: {', '.join(blocked)} (need browser fetch or a different URL)")

    if "--report" in sys.argv and new_items:
        print(f"news archive: +{new_items} items")
        try:
            with open(OUT, encoding="utf-8") as f:
                for line in f.readlines()[-5:]:
                    t = json.loads(line)
                    print(f"  [{t['source']}] {t['text'][:90]}")
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
