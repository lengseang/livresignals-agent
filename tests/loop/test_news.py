"""trading.news: RSS + Fear & Greed parsing, symbol tags, dedupe store, runner.

Fixtures in fixtures/news/ are real responses captured 2026-09-26 ~01:22 UTC from the
four feeds and api.alternative.me/fng/, trimmed to four verbatim <item> blocks each.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading.news import feeds
from trading.news.parse import FeedError, canonical_url, parse_feed, parse_fng, parse_published
from trading.news.run import main as run_main
from trading.news.run import run_once
from trading.news.store import NewsStore
from trading.news.tags import tag_symbols

FIX = Path(__file__).parent / "fixtures" / "news"
FETCHED = datetime(2026, 9, 26, 1, 22, 15, tzinfo=timezone.utc)
SOURCES = ("cointelegraph", "theblock", "decrypt", "coindesk")


def _body(name: str) -> bytes:
    return (FIX / f"{name}.xml").read_bytes()


def _payloads() -> dict[str, bytes]:
    out = {feeds.FEEDS[s]: _body(s) for s in SOURCES}
    out[feeds.FNG_URL] = (FIX / "fng.json").read_bytes()
    return out


# --- feed parsing -------------------------------------------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_each_real_feed_parses_four_items_with_utc_times(source):
    items = parse_feed(source, _body(source), FETCHED)
    assert len(items) == 4
    for it in items:
        assert it["kind"] == "news" and it["source"] == source
        assert it["url"].startswith("https://") and it["title"]
        assert it["published_status"] == "OK"
        assert it["published_utc"].endswith("Z")
        assert it["published_raw"].endswith("+0000")
        assert it["fetched_utc"] == "2026-09-26T01:22:15Z"
        assert isinstance(it["symbols"], list)


def test_published_time_is_taken_from_the_feed_not_the_fetch():
    first = parse_feed("coindesk", _body("coindesk"), FETCHED)[0]
    assert first["published_utc"] == "2026-09-25T22:17:38Z"
    assert first["published_raw"] == "Fri, 25 Sep 2026 22:17:38 +0000"


def test_cointelegraph_tracking_params_are_stripped_from_url():
    urls = [i["url"] for i in parse_feed("cointelegraph", _body("cointelegraph"), FETCHED)]
    assert urls[0] == "https://cointelegraph.com/news/og-com-seeks-cftc-approval-single-stock-perpetual-futures"
    assert not any("utm_" in u for u in urls)


def test_canonical_url_keeps_non_tracking_params_and_drops_fragment():
    assert canonical_url("HTTPS://Example.COM/a?id=3&utm_source=x#top") == "https://example.com/a?id=3"
    assert canonical_url("  https://x.io/p  ") == "https://x.io/p"


def test_feed_symbol_tags_from_real_items():
    by_title = {i["title"]: i["symbols"] for s in SOURCES for i in parse_feed(s, _body(s), FETCHED)}
    assert by_title["Sequans exits Bitcoin treasury strategy after selling remaining 314 BTC"] == ["BTC"]
    assert by_title["Bitcoin Rally Slows as $15.6 Billion Options Expiry Hits—XRP and Solana Keep Climbing"] == \
        ["BTC", "SOL", "XRP"]
    assert by_title["Magic Eden Warns Old Ethereum NFT Listings Are Exposed to Payment Processor Exploit"] == ["ETH"]
    # "Binance" is the exchange, not BNB
    assert by_title["Ethena expands USDe backing strategy into bStocks and equity perpetuals on Binance"] == []
    assert by_title["U.S. SEC's steadiest crypto advocate, Hester Peirce, to depart next week"] == []


def test_atom_feed_is_parsed_as_a_fallback():
    atom = (b'<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            b'<entry><title>Solana outage</title><link href="https://ex.com/a"/>'
            b'<updated>2026-09-25T10:00:00+02:00</updated></entry></feed>')
    [it] = parse_feed("x", atom, FETCHED)
    assert it["url"] == "https://ex.com/a" and it["symbols"] == ["SOL"]
    assert it["published_utc"] == "2026-09-25T08:00:00Z"


def test_item_without_link_is_skipped():
    rss = b"<rss><channel><item><title>BTC up</title></item></channel></rss>"
    assert parse_feed("x", rss, FETCHED) == []


def test_item_with_bad_date_is_unknown_never_invented():
    rss = (b"<rss><channel><item><title>t</title><link>https://ex.com/1</link>"
           b"<pubDate>yesterday-ish</pubDate></item>"
           b"<item><title>t2</title><link>https://ex.com/2</link></item></channel></rss>")
    a, b = parse_feed("x", rss, FETCHED)
    assert (a["published_utc"], a["published_status"], a["published_raw"]) == (None, "UNKNOWN", "yesterday-ish")
    assert (b["published_utc"], b["published_status"], b["published_raw"]) == (None, "UNKNOWN", None)


def test_doctype_and_entities_are_refused():
    evil = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel></channel></rss>'
    with pytest.raises(FeedError):
        parse_feed("x", evil, FETCHED)


def test_non_xml_body_raises_feed_error():
    with pytest.raises(FeedError):
        parse_feed("x", b"<html><body>consent page", FETCHED)


# --- published time -----------------------------------------------------------

# "CEST" is a regression case: email.utils silently maps unknown zone names to +0000.
@pytest.mark.parametrize("raw", [None, "", "   ", "not a date", "Fri, 25 Sep 2026 22:17:38",
                                 "Fri, 25 Sep 2026 22:17:38 CEST", "2026-09-25T22:17:38"])
def test_unparseable_or_zoneless_published_is_unknown(raw):
    assert parse_published(raw) == (None, "UNKNOWN")


@pytest.mark.parametrize("raw,expected", [
    ("Fri, 25 Sep 2026 22:17:38 +0000", "2026-09-25T22:17:38Z"),
    ("Fri, 25 Sep 2026 22:17:38 +0200", "2026-09-25T20:17:38Z"),
    ("Fri, 25 Sep 2026 22:17:38 GMT", "2026-09-25T22:17:38Z"),
    ("Fri, 25 Sep 2026 22:17:38 -0000", "2026-09-25T22:17:38Z"),
    ("2026-09-25T22:17:38Z", "2026-09-25T22:17:38Z"),
    ("2026-09-25T22:17:38-05:00", "2026-09-26T03:17:38Z"),
])
def test_published_is_converted_to_utc(raw, expected):
    assert parse_published(raw) == (expected, "OK")


# --- tagging --------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Bitcoin and ETH rally", ["BTC", "ETH"]),
    ("bitcoin's miners", ["BTC"]),
    ("$BTC breaks out", ["BTC"]),
    ("WBTC and stETH depeg", []),
    ("Chainlink oracle; LINK up", ["LINK"]),
    ("click the link below", []),
    ("near-term outlook, a dot on the chart, atom by atom", []),
    ("NEAR and Near Protocol", ["NEAR"]),
    ("Near the highs", []),
    ("DOT and Polkadot, ATOM and Cosmos", ["ATOM", "DOT"]),
    ("Ripple wins case", []),
    ("XRP ETF", ["XRP"]),
    ("Binance lists token", []),
    ("BNB Chain upgrade", ["BNB"]),
    ("Cardano, ADA, Dogecoin, DOGE, Avalanche AVAX, Sui SUI, Arbitrum ARB, Litecoin LTC",
     ["ADA", "ARB", "AVAX", "DOGE", "LTC", "SUI"]),
    ("Ether price", ["ETH"]),
    ("sui generis ruling", []),
])
def test_tag_symbols(text, expected):
    assert tag_symbols(text) == expected


# --- fear & greed -----------------------------------------------------------------

def test_fng_real_fixture():
    rec = parse_fng((FIX / "fng.json").read_bytes(), FETCHED)
    assert rec == {
        "kind": "fng", "source": "alternative.me", "value": 74, "classification": "Greed",
        "index_time_utc": "2026-09-26T00:00:00Z", "index_time_status": "OK",
        "time_until_update_s": 81471, "fetched_utc": "2026-09-26T01:22:15Z",
    }


def test_fng_missing_fields_are_null_and_unknown():
    rec = parse_fng(b'{"data":[{"value":"n/a"}]}', FETCHED)
    assert rec["value"] is None and rec["classification"] is None
    assert rec["index_time_utc"] is None and rec["index_time_status"] == "UNKNOWN"
    assert rec["time_until_update_s"] is None


def test_fng_malformed_raises():
    with pytest.raises(FeedError):
        parse_fng(b'{"data":[]}', FETCHED)
    with pytest.raises(FeedError):
        parse_fng(b"not json", FETCHED)


# --- store ------------------------------------------------------------------------

def test_store_dedupes_within_and_across_runs(tmp_path):
    items = parse_feed("decrypt", _body("decrypt"), FETCHED)
    s1 = NewsStore(tmp_path)
    assert s1.append(items + items) == 4
    s2 = NewsStore(tmp_path)                      # a later run re-reads the files
    assert s2.append(items) == 0
    lines = (tmp_path / "2026-09-26.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and all(json.loads(l)["kind"] == "news" for l in lines)


def test_store_file_is_named_by_fetch_date(tmp_path):
    later = datetime(2026, 9, 27, 0, 0, 1, tzinfo=timezone.utc)
    NewsStore(tmp_path).append(parse_feed("theblock", _body("theblock"), later))
    assert [p.name for p in tmp_path.glob("*.jsonl")] == ["2026-09-27.jsonl"]


def test_fng_dedupes_by_index_time_and_errors_are_never_deduped(tmp_path):
    fng = parse_fng((FIX / "fng.json").read_bytes(), FETCHED)
    err = {"kind": "fetch_error", "source": "x", "url": "u", "error": "e", "fetched_utc": "2026-09-26T01:22:15Z"}
    s = NewsStore(tmp_path)
    assert s.append([fng, fng, err, err]) == 3
    assert NewsStore(tmp_path).append([fng]) == 0


# --- runner -----------------------------------------------------------------------

def test_run_once_writes_all_sources(tmp_path):
    payloads = _payloads()
    stats = run_once(tmp_path, fetcher=payloads.__getitem__, now=lambda: FETCHED)
    assert stats == {"news_new": 16, "news_seen": 16, "fng_new": 1, "errors": 0}
    again = run_once(tmp_path, fetcher=payloads.__getitem__, now=lambda: FETCHED)
    assert again == {"news_new": 0, "news_seen": 16, "fng_new": 0, "errors": 0}


def test_one_failing_source_is_recorded_and_others_still_written(tmp_path):
    payloads = _payloads()

    def fetcher(url):
        if url == feeds.FEEDS["decrypt"]:
            raise OSError("boom")
        if url == feeds.FEEDS["theblock"]:
            return b"<rss><channel></channel></rss>"        # parses, zero items
        return payloads[url]

    stats = run_once(tmp_path, fetcher=fetcher, now=lambda: FETCHED)
    assert stats == {"news_new": 8, "news_seen": 8, "fng_new": 1, "errors": 2}
    rows = [json.loads(l) for l in (tmp_path / "2026-09-26.jsonl").read_text(encoding="utf-8").splitlines()]
    errs = {r["source"]: r["error"] for r in rows if r["kind"] == "fetch_error"}
    assert set(errs) == {"decrypt", "theblock"}
    assert "boom" in errs["decrypt"] and "no items" in errs["theblock"]


def test_cli_rejects_polling_faster_than_five_minutes(tmp_path):
    with pytest.raises(SystemExit):
        run_main(["--every-minutes", "4", "--out", str(tmp_path)])


def test_sources_are_the_four_documented_feeds():
    assert feeds.FEEDS == {
        "cointelegraph": "https://cointelegraph.com/rss",
        "theblock": "https://www.theblock.co/rss.xml",
        "decrypt": "https://decrypt.co/feed",
        "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    }
    assert feeds.FNG_URL == "https://api.alternative.me/fng/"


def test_news_package_never_imports_an_exchange_client():
    code = (
        "import sys, trading.news.run;"
        "bad=[m for m in sys.modules if m=='ccxt' or m.startswith('ccxt.') or m.startswith('trading.exchange')];"
        "print(','.join(bad))"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == ""


@pytest.mark.network
@pytest.mark.skipif(os.environ.get("LOOP_NETWORK") != "1", reason="live smoke test; set LOOP_NETWORK=1")
def test_live_smoke_every_source_returns_items(tmp_path):
    stats = run_once(tmp_path)
    assert stats["errors"] == 0 and stats["news_seen"] > 0
