"""Normalize RSS 2.0 / Atom items and the Fear & Greed response into flat records.

Times are never invented: a publish time is kept only if the feed gave one that parses
and carries a zone; otherwise published_utc is null and published_status is UNKNOWN.
The raw string is always kept in published_raw.
"""
from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_tz
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from trading.news.tags import tag_symbols

OK, UNKNOWN = "OK", "UNKNOWN"
MAX_BYTES = 5 * 1024 * 1024
_ATOM = "{http://www.w3.org/2005/Atom}"
_DC_DATE = "{http://purl.org/dc/elements/1.1/}date"
_TAG_RE = re.compile(r"<[^>]+>")
_FIRST_ELEMENT = re.compile(rb"<(?![?!])")
_DOCTYPE = re.compile(rb"<!(DOCTYPE|ENTITY)", re.IGNORECASE)
_RFC822_ZONE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?\s+([+-]\d{4}|[A-Za-z]{1,5})$")
# The alphabetic zones email.utils maps to real offsets (email/_parseaddr.py _timezones).
_KNOWN_ZONES = frozenset({"UT", "UTC", "GMT", "Z", "AST", "ADT", "EST", "EDT",
                          "CST", "CDT", "MST", "MDT", "PST", "PDT"})


class FeedError(ValueError):
    """The payload is not a feed we can read (format change, HTML page, hostile XML)."""


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_published(raw: str | None) -> tuple[str | None, str]:
    """RFC 822 or ISO 8601 -> ("YYYY-MM-DDTHH:MM:SSZ", "OK"), else (None, "UNKNOWN")."""
    if raw is None or not raw.strip():
        return None, UNKNOWN
    s = raw.strip()
    # email.utils.parsedate_tz reports offset 0 when the zone is missing *or* unrecognised
    # ("CEST"), which would silently turn local times into UTC. So we only trust it when the
    # string visibly ends in a numeric offset or a zone name it actually knows.
    m = _RFC822_ZONE.search(s)
    if m and (m.group(1)[0] in "+-" or m.group(1).upper() in _KNOWN_ZONES):
        try:
            t = parsedate_tz(s)
            naive = datetime(*t[:6]) if t else None
        except (TypeError, ValueError, IndexError, OverflowError):
            naive = None
        if naive is None or t[9] is None:
            return None, UNKNOWN
        return iso_utc(naive.replace(tzinfo=timezone.utc) - timedelta(seconds=t[9])), OK
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None, UNKNOWN
    if dt.tzinfo is None:
        return None, UNKNOWN
    return iso_utc(dt), OK


def canonical_url(url: str) -> str:
    """Lower-case scheme/host, drop the fragment and utm_* params; keep everything else."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_")])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def _text(s: str | None) -> str:
    return html.unescape(_TAG_RE.sub(" ", s or "")).strip()


def _clean(s: str | None) -> str | None:
    s = (s or "").strip()
    return s or None


def _root(body: bytes) -> ET.Element:
    if len(body) > MAX_BYTES:
        raise FeedError(f"payload larger than {MAX_BYTES} bytes")
    m = _FIRST_ELEMENT.search(body)
    prolog = body[: m.start()] if m else body
    if _DOCTYPE.search(prolog):
        raise FeedError("payload declares a DOCTYPE/ENTITY; refused")
    try:
        return ET.fromstring(body)
    except ET.ParseError as e:
        raise FeedError(f"not XML: {e}") from e


def _record(source: str, url: str, title: str | None, summary: str | None,
            pub_raw: str | None, fetched: datetime) -> dict:
    published_utc, status = parse_published(pub_raw)
    return {
        "kind": "news",
        "source": source,
        "url": canonical_url(url),
        "title": title,
        "published_utc": published_utc,
        "published_status": status,
        "published_raw": _clean(pub_raw),
        "fetched_utc": iso_utc(fetched),
        "symbols": tag_symbols(f"{title or ''}\n{_text(summary)}"),
    }


def parse_feed(source: str, body: bytes, fetched: datetime) -> list[dict]:
    """All items with a link, in feed order. Items without a link are skipped (no dedupe key)."""
    root = _root(body)
    out: list[dict] = []
    if root.tag == f"{_ATOM}feed":
        for e in root.iter(f"{_ATOM}entry"):
            link = None
            for ln in e.findall(f"{_ATOM}link"):
                if ln.get("rel", "alternate") == "alternate" and ln.get("href"):
                    link = ln.get("href")
                    break
            if not _clean(link):
                continue
            pub = e.findtext(f"{_ATOM}published") or e.findtext(f"{_ATOM}updated")
            out.append(_record(source, link, _clean(e.findtext(f"{_ATOM}title")),
                               e.findtext(f"{_ATOM}summary"), pub, fetched))
        return out
    for it in root.iter("item"):
        link = _clean(it.findtext("link"))
        if link is None:
            guid = it.find("guid")
            if guid is not None and guid.get("isPermaLink", "true") != "false" \
                    and (guid.text or "").strip().startswith("http"):
                link = guid.text.strip()
        if link is None:
            continue
        pub = it.findtext("pubDate") or it.findtext(_DC_DATE)
        out.append(_record(source, link, _clean(it.findtext("title")),
                           it.findtext("description"), pub, fetched))
    return out


def _int_or_none(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_fng(body: bytes, fetched: datetime) -> dict:
    """api.alternative.me/fng/ -> one record. Missing or non-numeric fields are null, never guessed."""
    try:
        obj = json.loads(body)
    except ValueError as e:
        raise FeedError(f"not JSON: {e}") from e
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise FeedError("no data[0] in Fear & Greed response")
    d = data[0]
    ts = _int_or_none(d.get("timestamp"))
    index_time = None
    if ts is not None:
        try:
            index_time = iso_utc(datetime.fromtimestamp(ts, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            index_time = None
    return {
        "kind": "fng",
        "source": "alternative.me",
        "value": _int_or_none(d.get("value")),
        "classification": _clean(d.get("value_classification") if isinstance(d.get("value_classification"), str) else None),
        "index_time_utc": index_time,
        "index_time_status": OK if index_time else UNKNOWN,
        "time_until_update_s": _int_or_none(d.get("time_until_update")),
        "fetched_utc": iso_utc(fetched),
    }
