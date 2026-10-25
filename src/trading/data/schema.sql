-- sqlite schema.sql for the Trading Agent ledger and raw market data.
-- Rules: idempotent creates, WAL mode, UTC ISO keys, source_id unique per feed.
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open_time_utc INTEGER NOT NULL,
    close_time_utc INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    trades INTEGER,
    taker_buy_volume REAL,
    event_time_utc INTEGER NOT NULL,
    received_time_utc INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    PRIMARY KEY (symbol, timeframe, open_time_utc)
);
CREATE INDEX IF NOT EXISTS idx_candles_time ON candles(symbol, timeframe, open_time_utc);

CREATE TABLE IF NOT EXISTS order_books (
    symbol TEXT NOT NULL,
    ts_utc INTEGER NOT NULL,
    bids TEXT NOT NULL,
    asks TEXT NOT NULL,
    received_time_utc INTEGER NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (symbol, ts_utc)
);

CREATE TABLE IF NOT EXISTS recent_trades (
    symbol TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    ts_utc INTEGER NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    is_buyer_maker INTEGER NOT NULL,
    received_time_utc INTEGER NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (symbol, trade_id)
);

CREATE TABLE IF NOT EXISTS ingestion_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc INTEGER NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT,
    error TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '1');
INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('created_at_utc', strftime('%s','now'));