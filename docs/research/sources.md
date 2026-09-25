# Data source inventory (legal, public)

Task T1.2 · 2026-09-25 · Every "reachable" status below was measured from the **dev machine**
(Windows, this network) with `curl` or an `aiohttp` WebSocket client at the time of writing.
The production VPS (AWS) is on a different network, so reachability there must be re-checked.

## Headline

- **Binance's main hosts are blocked from the dev machine** (`api.binance.com`, `api1.binance.com`,
  `fapi.binance.com`, `stream.binance.com:9443`, `www.binance.com`: all time out). Bybit, OKX and
  Coinbase time out too.
- **Binance's official market-data-only mirrors work:** `https://data-api.binance.vision` (REST)
  and `wss://data-stream.binance.vision` (WebSocket). A live `btcusdt@bookTicker` message was received.
  The docs say the mirror carries market data only, with no user data stream
  ([web-socket-streams.md](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md), line 50).
  That's exactly what paper scanning needs, and it can't be used to trade, which is a useful safety property.
- **Phase 2 is unblocked locally** via the `.vision` mirrors. Live futures data (funding, open interest)
  is not reachable locally; see below.

## Market data

| Source | Use | Reachable (dev) | Limits / terms (source) |
|---|---|---|---|
| `wss://data-stream.binance.vision` | per-second ticks: `@bookTicker`, `@aggTrade`, `@trade`, `@kline_1s`, `@depth<levels>` | **yes**, live message received | documented for `stream.binance.com`; the docs don't state separate limits for the mirror, so the loop **assumes the same**: 5 incoming msgs/s per connection; ≤1024 streams per connection; 300 connections per 5 min per IP; server pings every 20s and expects a pong within a minute; a connection lasts ≤24h ([ws docs](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md) lines 44–63). `1s` is a listed kline interval (line 303) |
| `https://data-api.binance.vision` | REST klines, exchange info, depth snapshots | **yes** (200) | the official "public market data" endpoint ([rest docs](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md), line 100); its request-weight limits weren't verified tonight, so read the `X-MBX-USED-WEIGHT` headers at runtime |
| `https://data.binance.vision` | historical bulk dumps (spot + futures klines, trades, **funding rate**) | **yes** (200) | daily/monthly zip files; for backtests, not live |
| `api.binance.com`, `stream.binance.com` | same as above, plus account/trading | **no** (timeout) | irrelevant for paper; prod uses them from the VPS |
| `fapi.binance.com` | live funding rate, open interest, liquidations | **no** (timeout) | would need the VPS or a different network |
| `futures.kraken.com` | live futures tickers (a funding/OI proxy on another venue) | **yes** (200) | public; a different venue, so its funding doesn't equal Binance's |
| `api.kraken.com` | spot REST | **yes** (200) | public |
| `api.bybit.com`, `ws.okx.com`, `api.exchange.coinbase.com` | alternatives | **no** | not usable from dev |

## News

| Source | Reachable | Notes |
|---|---|---|
| Cointelegraph RSS `cointelegraph.com/rss` | yes (200) | free, public feed |
| The Block RSS `theblock.co/rss.xml` | yes (200) | free, public feed |
| Decrypt RSS `decrypt.co/feed` | yes (200) | free, public feed |
| CoinDesk RSS `coindesk.com/arc/outboundfeeds/rss/` | yes (308 → 200) | follow the redirect |
| CryptoPanic API | 403 without a token | **free developer plan discontinued in early 2026**; paid only ([CryptoPanic API](https://cryptopanic.com/developers/api/)). Needs a paid signup, so **escalate, don't use** |
| Binance announcements page | no (binance.com blocked) | the public `t.me/s/binance_announcements` preview is reachable (200); see social |

RSS gives headlines and publish timestamps, typically delayed by minutes, so it isn't per-second. Use it for
**context and event tagging** (listings, hacks, regulation), not for latency-sensitive entries.

## Sentiment and on-chain

| Source | Reachable | Notes |
|---|---|---|
| Fear & Greed Index `api.alternative.me/fng/` | yes | free, **daily** resolution (returned `value: 71, Greed` with `time_until_update`); a regime input, not a trigger |
| CoinGecko `api.coingecko.com` | yes (200) | market cap, volume, trending; the public API is rate-limited (the exact current limit wasn't verified tonight) |
| mempool.space `mempool.space/api` | yes | BTC mempool and fees; a weak direct signal for spot, logged for context |

## Social and signal channels

| Source | Reachable | Assessment |
|---|---|---|
| Public Telegram channels via the web preview `t.me/s/<channel>` | yes (200) | public, read-only HTML. Legal to read, but automated scraping sits in Telegram's terms gray area. **Owner decision (escalate).** An alternative is the official Telegram API with the owner's own account |
| Reddit JSON | 403 | now requires OAuth app registration, so **escalate** |
| X/Twitter | not tested | the API is paid; out of scope |
| Paid/private signal groups, leaked feeds | n/a | **excluded**: not legal to use (the owner's original request; declined) |

## What each phase will use

- **Phase 2 ticks (T2.2, T2.3):** `data-stream.binance.vision`: `@bookTicker` (spread), `@aggTrade`
  (flow), `@kline_1s` for the paper universe, on one connection (≤1024 streams), reconnecting before the
  24h cut and answering pings.
- **Backtests:** `data.binance.vision` historical dumps (including the futures funding rate).
- **News (T2.4):** the four RSS feeds, deduped by URL, timestamped at fetch and publish time, symbol-tagged by keyword.
- **Regime inputs (T7.1):** Fear & Greed (daily), BTC trend (already in the repo).
- **Funding/OI:** deferred. Local dev has no live source; options are to run that ingester on the VPS (owner)
  or use Kraken futures as a proxy (documented as a different venue).

## Escalations raised

- CryptoPanic (paid), Reddit (OAuth app), Telegram channel reading (terms): all need the owner's decision or signup.
- Whether the VPS can reach `fapi.binance.com` for live funding/OI.
