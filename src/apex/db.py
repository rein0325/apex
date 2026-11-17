"""DuckDB 資料存取層。策略與模型只透過這一層讀寫資料，日後換資料庫不影響其他程式。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from pathlib import Path

import duckdb
import pandas as pd

from apex.sources import Quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS trading_days (
    date DATE PRIMARY KEY,
    source VARCHAR NOT NULL,
    fetched_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_months (
    month VARCHAR PRIMARY KEY,            -- 'YYYY-MM'
    n_days INTEGER NOT NULL,
    complete BOOLEAN NOT NULL,            -- 月份已結束才算完整；當月需重抓
    fetched_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_quotes (
    date DATE NOT NULL,
    market VARCHAR NOT NULL,              -- 'TWSE' | 'TPEX'
    code VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    sec_type VARCHAR NOT NULL,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,   -- 未還原；無成交為 NULL
    volume BIGINT,                        -- 股
    value BIGINT,                         -- 元
    trades BIGINT,
    is_managed BOOLEAN NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (date, market, code)
);
CREATE TABLE IF NOT EXISTS ingest_status (
    date DATE NOT NULL,
    market VARCHAR NOT NULL,
    status VARCHAR NOT NULL,              -- 'ok' | 'failed'
    n_rows INTEGER,
    message VARCHAR,
    raw_sha256 VARCHAR,
    updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (date, market)
);
CREATE TABLE IF NOT EXISTS dq_results (
    date DATE NOT NULL,
    market VARCHAR NOT NULL,              -- 'TWSE' | 'TPEX' | 'ALL'
    check_name VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,            -- 'info' | 'warning' | 'error'
    detail VARCHAR,
    checked_at TIMESTAMP NOT NULL,
    PRIMARY KEY (date, market, check_name)
);
CREATE TABLE IF NOT EXISTS securities (
    code VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    market VARCHAR NOT NULL,
    sec_type VARCHAR NOT NULL,
    first_date DATE NOT NULL,
    last_date DATE NOT NULL,
    n_days INTEGER NOT NULL
);
"""

MARKETS = ("TWSE", "TPEX")


def now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


class Database:
    def __init__(self, path: Path | str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path))
        self.con.execute(SCHEMA)

    def close(self) -> None:
        self.con.close()

    # --- 交易日曆 ---------------------------------------------------------

    def save_calendar_month(self, year: int, month: int, days: Iterable[dt.date], complete: bool,
                            source: str = "TWSE:MI_5MINS_HIST") -> None:
        days = sorted(days)
        ts = now()
        first = dt.date(year, month, 1)
        nxt = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        self.con.execute("BEGIN")
        try:
            self.con.execute("DELETE FROM trading_days WHERE date >= ? AND date < ?", [first, nxt])
            if days:
                self.con.executemany("INSERT INTO trading_days VALUES (?, ?, ?)",
                                     [(d, source, ts) for d in days])
            self.con.execute("INSERT OR REPLACE INTO calendar_months VALUES (?, ?, ?, ?)",
                             [f"{year:04d}-{month:02d}", len(days), complete, ts])
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def complete_months(self) -> set[str]:
        return {r[0] for r in self.con.execute(
            "SELECT month FROM calendar_months WHERE complete").fetchall()}

    def trading_days(self, start: dt.date, end: dt.date) -> list[dt.date]:
        sql = "SELECT date FROM trading_days WHERE date BETWEEN ? AND ? ORDER BY date"
        return [r[0] for r in self.con.execute(sql, [start, end]).fetchall()]

    def previous_trading_days(self, date: dt.date, n: int) -> list[dt.date]:
        return [r[0] for r in self.con.execute(
            "SELECT date FROM trading_days WHERE date < ? ORDER BY date DESC LIMIT ?", [date, n]).fetchall()]

    # --- 行情 ---------------------------------------------------------------

    def replace_quotes(self, date: dt.date, market: str, quotes: list[Quote]) -> None:
        """整日整市場替換，重複執行結果相同。

        以 DataFrame 整批寫入：DuckDB 的 executemany 逐筆執行，2,400 筆約需 6 秒。
        使用 pandas 可為空型別（Float64／Int64），確保無成交的 None 寫成 NULL 而非 NaN。
        """
        df = pd.DataFrame({
            "date": pd.Series([q.date for q in quotes], dtype="object"),
            "market": [q.market for q in quotes],
            "code": [q.code for q in quotes],
            "name": [q.name for q in quotes],
            "sec_type": [q.sec_type for q in quotes],
            **{col: pd.array([getattr(q, col) for q in quotes], dtype="Float64")
               for col in ("open", "high", "low", "close")},
            **{col: pd.array([getattr(q, col) for q in quotes], dtype="Int64")
               for col in ("volume", "value", "trades")},
            "is_managed": [q.is_managed for q in quotes],
            "fetched_at": now(),
        })
        self.con.execute("BEGIN")
        try:
            self.con.execute("DELETE FROM daily_quotes WHERE date = ? AND market = ?", [date, market])
            if quotes:
                self.con.register("_quotes_df", df)
                self.con.execute("""
                    INSERT INTO daily_quotes
                    SELECT CAST(date AS DATE), market, code, name, sec_type, open, high, low, close,
                           volume, value, trades, is_managed, fetched_at FROM _quotes_df
                """)
                self.con.unregister("_quotes_df")
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def set_status(self, date: dt.date, market: str, status: str, n_rows: int | None = None,
                   message: str | None = None, raw_sha256: str | None = None) -> None:
        self.con.execute("INSERT OR REPLACE INTO ingest_status VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [date, market, status, n_rows, message, raw_sha256, now()])

    def status(self, date: dt.date, market: str) -> str | None:
        row = self.con.execute("SELECT status FROM ingest_status WHERE date = ? AND market = ?",
                               [date, market]).fetchone()
        return row[0] if row else None

    # --- 品質檢查 -----------------------------------------------------------

    def save_dq(self, date: dt.date, results: list[tuple[str, str, str, str]]) -> None:
        """results：(market, check_name, severity, detail)。先清除該日舊結果。"""
        ts = now()
        self.con.execute("DELETE FROM dq_results WHERE date = ?", [date])
        if results:
            self.con.executemany("INSERT INTO dq_results VALUES (?, ?, ?, ?, ?, ?)",
                                 [(date, m, c, s, d, ts) for m, c, s, d in results])

    # --- 證券主檔 -----------------------------------------------------------

    def refresh_securities(self) -> int:
        """由 ok 日的行情重建證券主檔：最新名稱、市場、類別，首次／最後出現日。"""
        self.con.execute("""
            CREATE OR REPLACE TEMP VIEW ok_quotes AS
            SELECT q.* FROM daily_quotes q
            JOIN ingest_status s ON s.date = q.date AND s.market = q.market AND s.status = 'ok'
        """)
        self.con.execute("BEGIN")
        try:
            self.con.execute("DELETE FROM securities")
            self.con.execute("""
                INSERT INTO securities
                SELECT code,
                       arg_max(name, date), arg_max(market, date), arg_max(sec_type, date),
                       min(date), max(date), count(*)
                FROM ok_quotes GROUP BY code
            """)
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return self.con.execute("SELECT count(*) FROM securities").fetchone()[0]
