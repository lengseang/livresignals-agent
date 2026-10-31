#!/usr/bin/env python3
"""Multi-source research scraper: GitHub, arXiv, Hacker News (and X when given a session).

WHY THIS EXISTS
---------------
The question was "what is being built in trading automation". X cannot answer it
without credentials — its search redirects anonymous requests to a login wall and
returns 200 with 26 characters of content, which is the exact "success code, no
data" failure this stack has been bitten by before. So this starts with the sources
that publish openly, and is structured so X can be added as a fourth adapter the
moment a session cookie exists.

THE RULE THIS FILE IS BUILT AROUND
----------------------------------
Exit 0 is not success. Every adapter returns a count of RECORDS IT ACTUALLY PARSED,
and a run where a source returns zero usable records is recorded as FAILED even
when every HTTP status was 200. A source that silently returns nothing is worse than
one that errors, because nobody investigates it.

WHAT IT DOES NOT DO
-------------------
  * No credentials, no API keys, no account of any kind for the free three.
  * No orders, no trading state, no live path. It writes an archive.
  * No invented fields: a missing star count is None, never 0.

USAGE
-----
    # discover what the keywords actually surface, without committing anything
    python3 research_scraper.py --probe

    # full run with the default keyword set
    python3 research_scraper.py

    # your own keywords
    python3 research_scraper.py --keywords "funding arbitrage" "market making"

    # X, once a session cookie file exists (see --help-x)
    python3 research_scraper.py --x-cookies /root/x-session.json --keywords "trading bot"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path("/opt/trading-agent")
ARCHIVE = BASE / "data" / "research-archive.jsonl"
STATE = BASE / "data" / "research-scraper-state.json"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Keywords are deliberately split: TOOLS looks for software, CONCEPTS looks for
# ideas. Blending them returns marketing ("best AI trading bot 2026") alongside
# real work and the marketing always wins on volume.
KEYWORDS_TOOLS = [
    "trading bot", "algorithmic trading", "backtesting engine",
    "crypto trading bot", "market making bot", "execution algorithm",
    "order book", "funding rate arbitrage",
]
KEYWORDS_CONCEPTS = [
    "walk forward optimization", "look-ahead bias", "deflated sharpe ratio",
    "regime detection", "position sizing", "transaction cost model",
    "market microstructure", "factor model crypto",
]

DEFAULT_KEYWORDS = KEYWORDS_TOOLS + KEYWORDS_CONCEPTS


# ---------------------------------------------------------------- plumbing
def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def rec_id(source: str, key: str) -> str:
    return hashlib.sha256(f"{source}|{key}".encode()).hexdigest()[:16]


def http_json(url: str, timeout: int = 30, headers: dict | None = None) -> tuple[int, object]:
    """GET returning (status, parsed_json_or_None). Uses curl_cffi for TLS parity."""
    from curl_cffi import requests as creq
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    try:
        r = creq.get(url, headers=h, timeout=timeout, impersonate="chrome")
        if r.status_code != 200:
            return r.status_code, None
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, None
    except Exception as e:
        return -1, {"_error": str(e)[:200]}


def load_seen() -> set[str]:
    if not ARCHIVE.exists():
        return set()
    seen = set()
    with ARCHIVE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                seen.add(json.loads(line)["id"])
            except Exception:
                continue
    return seen


def append(recs: list[dict]) -> int:
    if not recs:
        return 0
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    with ARCHIVE.open("a") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(recs)


# ---------------------------------------------------------------- adapters
def scrape_github(keyword: str, since_days: int = 730, per_page: int = 20,
                  seen: set[str] | None = None) -> tuple[list[dict], str]:
    """Repositories pushed recently, ranked by stars.

    'pushed:>=' is the field that matters: a 40k-star repo untouched for three years
    is a museum piece, not a thing being built. Recency first, stars second.
    """
    seen = seen or set()
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")
    q = urllib.parse.quote(f"{keyword} pushed:>={since}")
    url = (f"https://api.github.com/search/repositories?q={q}"
           f"&sort=stars&order=desc&per_page={per_page}")
    status, data = http_json(url)
    if not isinstance(data, dict):
        return [], f"HTTP {status}, no JSON"
    if data.get("message"):                       # rate limit or auth error
        return [], f"API said: {data['message'][:80]}"
    items = data.get("items") or []
    out = []
    for r in items:
        rid = rec_id("github", r.get("full_name") or "")
        if rid in seen:
            continue
        out.append({
            "id": rid, "source": "github", "kind": "tool",
            "keyword": keyword, "scraped_utc": now_utc(),
            "title": r.get("full_name"),
            "url": r.get("html_url"),
            "summary": (r.get("description") or "")[:300],
            "stars": r.get("stargazers_count"),
            "language": r.get("language"),
            "pushed_utc": r.get("pushed_at"),
            "created_utc": r.get("created_at"),
            "topics": r.get("topics") or [],
            "license": ((r.get("license") or {}) or {}).get("spdx_id"),
        })
    return out, ("ok" if items else "no items")


def scrape_arxiv(keyword: str, max_results: int = 20,
                 seen: set[str] | None = None) -> tuple[list[dict], str]:
    """q-fin papers matching the keyword, newest first.

    Uses the export API so we get structured authors and dates rather than having to
    parse a listing page whose markup changes.
    """
    seen = seen or set()
    q = urllib.parse.quote(f'all:"{keyword}" AND cat:q-fin.*')
    url = (f"http://export.arxiv.org/api/query?search_query={q}"
           f"&start=0&max_results={max_results}&sortBy=submittedDate&sortOrder=descending")
    import xml.etree.ElementTree as ET
    from curl_cffi import requests as creq
    try:
        r = creq.get(url, headers={"User-Agent": UA}, timeout=40, impersonate="chrome")
    except Exception as e:
        return [], f"fetch failed: {str(e)[:80]}"
    if r.status_code != 200:
        return [], f"HTTP {r.status_code}"
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(r.text)
    except Exception as e:
        return [], f"XML parse failed: {str(e)[:60]}"
    entries = root.findall("a:entry", ns)
    out = []
    for e in entries:
        link = (e.find("a:id", ns).text or "").strip() if e.find("a:id", ns) is not None else ""
        rid = rec_id("arxiv", link)
        if rid in seen or not link:
            continue
        out.append({
            "id": rid, "source": "arxiv", "kind": "concept",
            "keyword": keyword, "scraped_utc": now_utc(),
            "title": " ".join(((e.find("a:title", ns).text if e.find("a:title", ns) is not None else "") or "").split()),
            "url": link.replace("http://", "https://"),
            "summary": " ".join(((e.find("a:summary", ns).text if e.find("a:summary", ns) is not None else "") or "").split())[:400],
            "published_utc": (e.find("a:published", ns).text if e.find("a:published", ns) is not None else None),
            "authors": [a.find("a:name", ns).text for a in e.findall("a:author", ns)
                        if a.find("a:name", ns) is not None][:5],
        })
    return out, ("ok" if entries else "no entries")


def scrape_hn(keyword: str, max_results: int = 20,
              seen: set[str] | None = None) -> tuple[list[dict], str]:
    """Hacker News stories via Algolia — real JSON, not HTML."""
    seen = seen or set()
    q = urllib.parse.quote(keyword)
    url = (f"https://hn.algolia.com/api/v1/search?query={q}&tags=story"
           f"&hitsPerPage={max_results}")
    status, data = http_json(url)
    if not isinstance(data, dict):
        return [], f"HTTP {status}, no JSON"
    hits = data.get("hits") or []
    out = []
    for h in hits:
        key = str(h.get("objectID") or "")
        rid = rec_id("hn", key)
        if rid in seen or not key or not h.get("title"):
            continue
        out.append({
            "id": rid, "source": "hn", "kind": "discussion",
            "keyword": keyword, "scraped_utc": now_utc(),
            "title": h.get("title"),
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={key}",
            "hn_url": f"https://news.ycombinator.com/item?id={key}",
            "points": h.get("points"),
            "comments": h.get("num_comments"),
            "author": h.get("author"),
            "created_utc": h.get("created_at"),
        })
    return out, ("ok" if hits else "no hits")


def scrape_x(keyword: str, cookies_path: str, max_results: int = 30,
             seen: set[str] | None = None) -> tuple[list[dict], str]:
    """X search using a logged-in session cookie.

    Requires a cookies file exported from a real logged-in browser session. Without
    one X redirects to a login wall and returns a 200 with no results, so this refuses
    rather than reporting an empty success.
    """
    seen = seen or set()
    p = Path(cookies_path).expanduser()
    if not p.exists():
        return [], f"no cookie file at {p}"
    try:
        cookies = json.loads(p.read_text())
    except Exception as e:
        return [], f"cookie file unreadable: {str(e)[:60]}"
    if isinstance(cookies, dict) and "cookies" in cookies:
        cookies = cookies["cookies"]
    if not isinstance(cookies, list) or not cookies:
        return [], "cookie file has no cookies list"

    try:
        from scrapling.fetchers import Fetcher
    except Exception as e:
        return [], f"scrapling unavailable: {str(e)[:60]}"

    jar = {c.get("name"): c.get("value") for c in cookies
           if c.get("name") and c.get("value")}
    if not any(k in jar for k in ("auth_token", "ct0")):
        return [], "cookies lack auth_token/ct0 — not a logged-in X session"

    url = ("https://x.com/search?q=" + urllib.parse.quote(keyword)
           + "&f=live&src=typed_query")
    try:
        page = Fetcher.get(url, impersonate="chrome", cookies=jar, timeout=60)
    except Exception as e:
        return [], f"fetch failed: {str(e)[:100]}"

    txt = page.get_all_text() or ""
    # The failure this guards against: a login wall renders 200 with a few dozen
    # characters. Check content, not status.
    if len(txt) < 2000:
        return [], f"login wall (only {len(txt)} chars returned)"
    arts = page.css("article")
    if not arts:
        return [], "no <article> blocks — layout changed or blocked"
    out = []
    for a in arts:
        try:
            hrefs = [x.attrib.get("href", "") for x in a.css("a")]
            link = next((h for h in hrefs if "/status/" in h), "")
            text = " ".join((a.get_all_text() or "").split())[:500]
            if not link or not text:
                continue
            rid = rec_id("x", link)
            if rid in seen:
                continue
            out.append({
                "id": rid, "source": "x", "kind": "post",
                "keyword": keyword, "scraped_utc": now_utc(),
                "title": text[:120],
                "url": "https://x.com" + link if link.startswith("/") else link,
                "summary": text,
            })
        except Exception:
            continue
    return out, ("ok" if out else "articles found but none parsed")


# ---------------------------------------------------------------- runner
def run(keywords: list[str], sources: list[str], per_source: int = 20,
        x_cookies: str | None = None, probe: bool = False) -> dict:
    seen = load_seen()
    results: dict[str, dict] = {}
    all_new: list[dict] = []

    for src in sources:
        collected: list[dict] = []
        notes: list[str] = []
        for kw in keywords:
            if src == "github":
                recs, note = scrape_github(kw, per_page=per_source, seen=seen | {r["id"] for r in collected})
            elif src == "arxiv":
                recs, note = scrape_arxiv(kw, max_results=per_source, seen=seen | {r["id"] for r in collected})
            elif src == "hn":
                recs, note = scrape_hn(kw, max_results=per_source, seen=seen | {r["id"] for r in collected})
            elif src == "x":
                if not x_cookies:
                    recs, note = [], "skipped: no --x-cookies"
                else:
                    recs, note = scrape_x(kw, x_cookies, max_results=per_source,
                                          seen=seen | {r["id"] for r in collected})
            else:
                recs, note = [], "unknown source"
            collected.extend(recs)
            if note != "ok":
                notes.append(f"{kw}: {note}")
            time.sleep(0.4)                     # be polite to free endpoints

        # A source that parsed ZERO records has failed, regardless of HTTP status.
        status = "ok" if collected else "FAILED"
        results[src] = {
            "status": status,
            "records": len(collected),
            "notes": notes[:6],
        }
        all_new.extend(collected)

    written = 0 if probe else append(all_new)

    state = {
        "last_run_utc": now_utc(),
        "keywords": keywords,
        "sources": results,
        "written": written,
        "probe_only": probe,
    }
    if not probe:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    return state


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Scrape GitHub, arXiv and Hacker News (and X with a session) "
                    "for trading-automation tools and concepts.")
    ap.add_argument("--keywords", nargs="*", default=None)
    ap.add_argument("--sources", nargs="*", default=["github", "arxiv", "hn"],
                    choices=["github", "arxiv", "hn", "x"])
    ap.add_argument("--per-source", type=int, default=20)
    ap.add_argument("--x-cookies", default=None,
                    help="path to a JSON cookie file exported from a logged-in X session")
    ap.add_argument("--probe", action="store_true",
                    help="show what the keywords surface without writing to the archive")
    ap.add_argument("--help-x", action="store_true",
                    help="print how to export X cookies")
    args = ap.parse_args()

    if args.help_x:
        print(__doc__)
        print("""
HOW TO EXPORT X COOKIES
-----------------------
You do NOT give anyone your password. You copy the session cookie from a browser you
are already logged into, and you can revoke it at any time by logging out everywhere.

1. Log in to https://x.com in a normal browser on your own machine.

2. Install a cookie-export extension (e.g. "Cookie-Editor" for Chrome/Firefox).

3. On the x.com tab, open the extension and press Export. For Chrome's
   Cookie-Editor choose "Export as JSON". You get a JSON array.

4. The two cookies that matter are auth_token and ct0. Confirm both are present.

5. Save the file on this box, readable only by root:

       # paste the JSON, then Ctrl-D
       cat > /root/x-session.json
       chmod 600 /root/x-session.json

6. Run:

       /home/ubuntu/hermes-venv/bin/python /opt/trading-agent/scripts/research_scraper.py \\
         --sources x --keywords "trading bot" --probe

SAFETY NOTES
------------
* Treat the cookie file like a password: it IS one. chmod 600, never commit it.
* Logging out of X everywhere invalidates it immediately.
* Scraping while logged in can get an ACCOUNT limited. Use your own account, keep
  the request rate low (this script sleeps between calls), and do not run it hourly.
* A brand-new account is far more likely to be flagged than an established one, so
  creating a fresh account for this is the WORST option, not the safest.
""")
        return 0

    keywords = args.keywords or DEFAULT_KEYWORDS
    print("=" * 72)
    print(f"RESEARCH SCRAPE — {len(keywords)} keywords × {len(args.sources)} source(s)")
    print(f"  keywords: {', '.join(keywords[:4])}{' …' if len(keywords) > 4 else ''}")
    print("=" * 72)

    state = run(keywords, args.sources, per_source=args.per_source,
                x_cookies=args.x_cookies, probe=args.probe)

    for src, info in state["sources"].items():
        mark = "OK    " if info["status"] == "ok" else "FAILED"
        print(f"  {mark} {src:8} {info['records']:4} records")
        for n in info["notes"][:3]:
            print(f"           {n}")

    total = sum(i["records"] for i in state["sources"].values())
    print(f"\n  {total} records")
    if state["probe_only"]:
        print("  PROBE — nothing written. Drop --probe to append to the archive.")
    else:
        print(f"  {state['written']} new appended to {ARCHIVE}")

    failed = [s for s, i in state["sources"].items() if i["status"] != "ok"]
    if failed:
        print(f"\n  ⚠ FAILED sources: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
