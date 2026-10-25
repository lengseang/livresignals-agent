"""SQLite store for normalized market data (idempotent, WAL)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from trading.config import SQLITE_PATH
from trading.data.normalize import _interval_ms as interval_ms


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or SQLITE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.row_factory = sqlite3.Row
    return con


def apply_schema(con: sqlite3.Connection) -> None:
    schema = Path(__file__).with_name("schema.sql")
    con.executescript(schema.read_text(encoding="utf-8"))
    con.commit()


def init(db_path: Path | None = None) -> sqlite3.Connection:
    con = connect(db_path)
    apply_schema(con)
    return con


def upsert_candle(con: sqlite3.Connection, row: dict,
                  now_ms: int | None = None) -> bool:
    """Write one normalized candle. Returns True if the row is now in the store
    (inserted or corrected), False if it was refused as still forming.

    A bool, not a count: SQLite's rowcount for an upsert depends on how the
    statement was compiled and the sqlite version, and a changed-row count is
    the wrong unit for a function whose callers want "did it take".

    THE STORE IS WHERE A PARTIAL BAR IS SETTLED, NOT EACH READER
    ------------------------------------------------------------
    The fetcher hands raw exchange rows, and Binance's array includes the
    in-progress candle. `normalize_candle` drops one when its clock says the
    window has not ended, but the write used to be INSERT OR IGNORE on
    (symbol, timeframe, open_time_utc) — the first write of a key won forever.
    Those are not the same rule, and the ledger shows the gap: at 2026-10-25
    01:00 UTC there were still 210 bars per 15m/30m symbol captured <10% into
    their own window, plus 10 each for 1d/4h/1h and 63 for 5m.

    So:
      * a row whose window has NOT ended is never persisted (no partial bar
        enters the table, whatever the fetcher returns);
      * a row whose window HAS ended is final, and final data beats a stored
        row that was captured earlier in its window — the legacy frozen bar is
        repaired in place instead of shadowing the true close;
      * a stored row that was already complete is left untouched, because
        `received_time_utc` is evidence of when the system first saw the bar
        and re-ingesting a closed bar must not keep moving it.

    `now_ms` is the caller's clock, defaulted to the wall clock. Tests use it
    to sit on either side of a bar's close without sleeping.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    span = interval_ms(row["timeframe"])
    if row["open_time_utc"] + span > now_ms:
        return False                                # still forming — never store

    cur = con.execute(
        """INSERT INTO candles(
            symbol,timeframe,open_time_utc,close_time_utc,
            open,high,low,close,volume,trades,taker_buy_volume,
            event_time_utc,received_time_utc,source,source_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol,timeframe,open_time_utc) DO UPDATE SET
            close_time_utc=excluded.close_time_utc,
            open=excluded.open, high=excluded.high,
            low=excluded.low, close=excluded.close,
            volume=excluded.volume, trades=excluded.trades,
            taker_buy_volume=excluded.taker_buy_volume,
            event_time_utc=excluded.event_time_utc,
            received_time_utc=excluded.received_time_utc,
            source=excluded.source, source_id=excluded.source_id
        WHERE candles.received_time_utc < excluded.close_time_utc""",
        (
            row["symbol"], row["timeframe"], row["open_time_utc"], row["close_time_utc"],
            row["open"], row["high"], row["low"], row["close"],
            row["volume"], row.get("trades"), row.get("taker_buy_volume"),
            row["event_time_utc"], row["received_time_utc"], row["source"],
            row["source_id"],
        ),
    )
    return cur.rowcount >= 1


def record_error(con: sqlite3.Connection, source: str, symbol: str | None, error: str) -> None:
    con.execute(
        "INSERT INTO ingestion_errors(ts_utc,source,symbol,error) VALUES(?,?,?,?)",
        (int(time.time()), source, symbol, error[:1000]),
    )
    con.commit()


def storage_check(con: sqlite3.Connection) -> dict:
    """Storage part of the health report, and it must be able to FAIL.

    The old version returned `{groups, duplicate_keys}` and nothing ever
    looked at it. It also could not see the failure that actually bit us:
    in WAL mode a NEW read connection can see the last committed frame but
    not the writer's uncommitted one, so "no data"/"stale" can be a property
    of the READER, not the database. So this checks the reader too:

      - journal mode actually in force
      - whether a WAL sidecar exists that this connection did not open
      - write/rollback probe (proves the file is writable by this process)
      - duplicate candle keys (the dedupe invariant on disk)

    Never raises: every failure becomes a problem string, because a health
    check that throws is a health check that reports nothing.
    """
    problems: list[str] = []
    info: dict = {"journal_mode": None, "wal_size": None, "writable": None,
                  "duplicate_keys": None}
    try:
        info["journal_mode"] = con.execute("PRAGMA journal_mode").fetchone()[0]
        try:
            path = Path(con.execute("PRAGMA database_list").fetchone()[2])
            wal = path.with_name(path.name + "-wal")
            info["wal_size"] = wal.stat().st_size if wal.exists() else 0
        except (IndexError, OSError, TypeError):
            pass
        try:
            con.execute("CREATE TABLE IF NOT EXISTS _health_probe (x INTEGER)")
            con.execute("INSERT INTO _health_probe VALUES (1)")
            con.rollback()
            info["writable"] = True
        except sqlite3.Error as e:
            info["writable"] = False
            problems.append(f"not writable: {e}")
        info["duplicate_keys"] = con.execute(
            "SELECT COUNT(*) AS n FROM (SELECT symbol,timeframe,open_time_utc FROM candles "
            "GROUP BY symbol,timeframe,open_time_utc HAVING COUNT(*)>1)"
        ).fetchone()[0]
        if info["duplicate_keys"]:
            problems.append(f"{info['duplicate_keys']} duplicate candle keys")
        info["groups"] = [dict(r) for r in con.execute(
            "SELECT symbol,timeframe,COUNT(*) AS n,COUNT(DISTINCT open_time_utc) AS uniq,"
            "MIN(open_time_utc) AS mn,MAX(open_time_utc) AS mx FROM candles "
            "GROUP BY symbol,timeframe").fetchall()]
    except sqlite3.Error as e:
        problems.append(f"storage unreadable: {e}")
    info["ok"] = not problems
    info["problems"] = problems
    return info