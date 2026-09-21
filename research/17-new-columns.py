"""NEW COLUMNS — what information exists that we do NOT already have?

Ranked by whether it is genuinely NEW (not price-derived, not redundant):
  TIER 1  positioning/flow  - on-chain netflow, whale moves, options skew,
                              liquidations. Real flow, not price.
  TIER 2  events            - exchange listings (documented pump effect)
  TIER 3  sentiment         - we already tested: redundant with momentum (R013)

Probe each for free/accessible access. If an API exists, no scraper needed.
"""
import json
import time
import urllib.request

H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def probe(name, url, note="", headers=None):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=headers or H)
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(6000)
            ms = (time.time() - t0) * 1000
            try:
                d = json.loads(body)
                shape = (list(d.keys())[:5] if isinstance(d, dict)
                         else f"list[{len(d)}]")
            except Exception:
                shape = f"non-JSON {len(body)}B"
            print(f"  ✅ {name:26} {r.status} {ms:>6.0f}ms  {str(shape)[:56]}")
            if note:
                print(f"     └─ {note}")
            return True
    except Exception as e:
        print(f"  ❌ {name:26} {type(e).__name__}: {str(e)[:52]}")
        return False


print("=" * 96)
print("TIER 1 — POSITIONING / FLOW (genuinely new information)")
print("=" * 96)
probe("Deribit options (skew)",
      "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option",
      "put/call skew = forward-looking positioning. NOT price-derived.")
probe("Deribit DVOL (implied vol)",
      "https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd",
      "implied vol index — fear gauge")
probe("Binance liquidations (allForceOrders)",
      "https://fapi.binance.com/fapi/v1/allForceOrders?symbol=BTCUSDT&limit=5",
      "cascade detection")
probe("Binance OI history",
      "https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1d&limit=5",
      "positioning; ~30d history only")
probe("Binance long/short ratio",
      "https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=1d&limit=5",
      "crowd positioning — a real contrarian input")
probe("Binance taker flow",
      "https://fapi.binance.com/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=1d&limit=5",
      "aggressive buy vs sell pressure")
probe("DefiLlama stablecoins",
      "https://stablecoins.llama.fi/stablecoins?includePrices=false",
      "stablecoin supply = dry powder / liquidity")
probe("DefiLlama chains TVL",
      "https://api.llama.fi/v2/chains",
      "capital rotation")

print()
print("=" * 96)
print("TIER 2 — EVENTS")
print("=" * 96)
probe("Binance announcements RSS",
      "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=5",
      "NEW LISTINGS — documented pump-then-fade effect")
probe("Binance listing page",
      "https://www.binance.com/en/support/announcement/list/48")

print()
print("=" * 96)
print("TIER 3 — SENTIMENT (we already proved redundant, R013)")
print("=" * 96)
probe("Reddit r/cryptocurrency",
      "https://www.reddit.com/r/cryptocurrency/hot.json?limit=5",
      "needs UA; rate limited hard")
probe("CoinGecko trending",
      "https://api.coingecko.com/api/v3/search/trending",
      "retail attention")

print()
print("=" * 96)
print("HISTORY DEPTH — how far back can we actually get?")
print("=" * 96)
probe("Binance daily klines (1000 max)",
      "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=1000")
probe("Binance monthly 2017 start",
      "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&startTime=1483228800000&limit=10")
probe("Bybit funding history",
      "https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&limit=200")
