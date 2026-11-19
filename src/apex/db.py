"""DuckDB 資料存取層。策略與模型只透過這一層讀寫資料，日後換資料庫不影響其他程式。"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from pathlib import Path

import duckdb
import pandas as pd

from apex.sources import Quote
from apex.sources.actions import Action
from apex.sources.fundamentals import FinRow, Revenue
from apex.sources.listings import Delisting, Listing

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
ALTER TABLE securities ADD COLUMN IF NOT EXISTS status VARCHAR;         -- 'listed' | 'delisted' | 'ended'
ALTER TABLE securities ADD COLUMN IF NOT EXISTS delisted_date DATE;
ALTER TABLE securities ADD COLUMN IF NOT EXISTS industry VARCHAR;
ALTER TABLE securities ADD COLUMN IF NOT EXISTS listed_date DATE;       -- 目前市場的上市（櫃）日
ALTER TABLE securities ADD COLUMN IF NOT EXISTS board VARCHAR;
CREATE TABLE IF NOT EXISTS listing_snapshots (
    month VARCHAR NOT NULL,               -- 'YYYY-MM'：每月保存一份 ISIN 一覽表
    code VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    market VARCHAR NOT NULL,
    board VARCHAR NOT NULL,               -- 上市、上市臺灣創新板、上櫃
    section VARCHAR NOT NULL,             -- 股票、ETF、特別股…
    industry VARCHAR,
    listed_date DATE,
    isin VARCHAR NOT NULL,
    cfi VARCHAR NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (month, code)
);
CREATE TABLE IF NOT EXISTS delistings (
    date DATE NOT NULL,                   -- 終止上市（櫃）日期
    market VARCHAR NOT NULL,
    code VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    reason VARCHAR,                       -- 只有上櫃提供（法規條文）
    source VARCHAR NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (date, market, code)
);
CREATE TABLE IF NOT EXISTS corporate_actions (
    date DATE NOT NULL,                   -- 除權息日或恢復買賣日（事件生效的第一個交易日）
    market VARCHAR NOT NULL,
    code VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,                -- 'dividend' | 'reduction' | 'par_change' | 'split'
    name VARCHAR NOT NULL,
    prev_close DOUBLE,                    -- 事件前收盤價
    ref_price DOUBLE,                     -- 還原用參考價（除權息為減除股利參考價）
    note VARCHAR,
    source VARCHAR NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    theo_ref DOUBLE,                      -- 含現增權值的理論參考價；跌停以此計算
    PRIMARY KEY (date, market, code, kind)
);
ALTER TABLE corporate_actions ADD COLUMN IF NOT EXISTS theo_ref DOUBLE;
CREATE TABLE IF NOT EXISTS institutional (   -- 三大法人買賣超，單位：股
    date DATE NOT NULL, market VARCHAR NOT NULL, code VARCHAR NOT NULL,
    foreign_buy BIGINT, foreign_sell BIGINT,
    foreign_net BIGINT,                   -- 外資及陸資合計（含外資自營商）
    foreign_dealer_net BIGINT,            -- 外資自營商；證交所 2017-12-18 前為 NULL
    trust_buy BIGINT, trust_sell BIGINT, trust_net BIGINT,
    dealer_net BIGINT, dealer_self_net BIGINT, dealer_hedge_net BIGINT,
    total_net BIGINT,
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (date, market, code)
);
CREATE TABLE IF NOT EXISTS margin (   -- 融資融券，單位：張
    date DATE NOT NULL, market VARCHAR NOT NULL, code VARCHAR NOT NULL,
    margin_buy BIGINT, margin_sell BIGINT, margin_repay BIGINT, margin_balance BIGINT, margin_limit BIGINT,
    short_sell BIGINT, short_buy BIGINT, short_repay BIGINT, short_balance BIGINT, short_limit BIGINT,
    "offset" BIGINT,                      -- 資券互抵
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (date, market, code)
);
CREATE TABLE IF NOT EXISTS monthly_revenue (   -- 月營收，單位：元
    year INTEGER NOT NULL, month INTEGER NOT NULL, code VARCHAR NOT NULL,
    market VARCHAR NOT NULL, name VARCHAR NOT NULL,
    industry VARCHAR,                     -- 公告當時的產業別
    revenue BIGINT, revenue_last_year BIGINT, cum_revenue BIGINT, cum_revenue_last_year BIGINT,
    note VARCHAR,
    available_date DATE NOT NULL,         -- 法定公告期限次日；回測只能用 available_date <= 訊號日
    source VARCHAR NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (year, month, code)
);
CREATE TABLE IF NOT EXISTS fin_income (   -- 綜合損益表，年初至今累計；金額為元、EPS 為元
    year INTEGER NOT NULL, quarter INTEGER NOT NULL, code VARCHAR NOT NULL,
    market VARCHAR NOT NULL, name VARCHAR NOT NULL, layout VARCHAR NOT NULL,
    revenue DOUBLE, gross_profit DOUBLE, operating_income DOUBLE, pretax_income DOUBLE,
    net_income DOUBLE, net_income_parent DOUBLE, eps DOUBLE,
    source VARCHAR NOT NULL, fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (year, quarter, code)
);
CREATE TABLE IF NOT EXISTS fin_balance (   -- 資產負債表（季底時點）
    year INTEGER NOT NULL, quarter INTEGER NOT NULL, code VARCHAR NOT NULL,
    market VARCHAR NOT NULL, name VARCHAR NOT NULL, layout VARCHAR NOT NULL,
    current_assets DOUBLE, total_assets DOUBLE, current_liabilities DOUBLE, total_liabilities DOUBLE,
    share_capital DOUBLE, equity_parent DOUBLE, equity DOUBLE, bvps DOUBLE,
    source VARCHAR NOT NULL, fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (year, quarter, code)
);
CREATE TABLE IF NOT EXISTS period_sync (   -- 月營收、財報已同步的期間
    source VARCHAR NOT NULL,
    period VARCHAR NOT NULL,              -- 'YYYYMM_0' | 'YYYYQn'
    n_rows INTEGER NOT NULL,
    complete BOOLEAN NOT NULL,            -- 公告期限過後 30 天才算完整，之前每次重抓
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (source, period)
);
-- 一覽表與月營收都沒有產業別時的備援（FinMind，含已下市公司）
CREATE TABLE IF NOT EXISTS industry_fallback (
    code VARCHAR PRIMARY KEY,
    industry VARCHAR NOT NULL,
    source VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS market_holidays (   -- 證交所公布的開休市日期（含未來年度）
    date DATE PRIMARY KEY,
    name VARCHAR NOT NULL,
    closed BOOLEAN NOT NULL,              -- 「開始交易日」「最後交易日」等為 false
    fetched_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS index_daily (   -- 市場指數：加權指數（開高低收）、加權報酬指數（收盤）
    date DATE NOT NULL,
    index_code VARCHAR NOT NULL,          -- 'TAIEX' | 'TAIEX_TR'
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    PRIMARY KEY (date, index_code)
);
CREATE TABLE IF NOT EXISTS dataset_status (   -- 日行情以外的每日資料集擷取狀態
    dataset VARCHAR NOT NULL,             -- 'institutional' | 'margin'
    date DATE NOT NULL,
    market VARCHAR NOT NULL,
    status VARCHAR NOT NULL,              -- 'ok' | 'failed'
    n_rows INTEGER,
    message VARCHAR,
    updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (dataset, date, market)
);
-- 公告的事件日遇休市（如颱風）時，實際生效日為之後第一個交易日。
CREATE OR REPLACE VIEW corporate_actions_eff AS
SELECT a.*, t.date AS eff_date
FROM corporate_actions a ASOF LEFT JOIN trading_days t ON a.date <= t.date;
CREATE TABLE IF NOT EXISTS action_sync (
    source VARCHAR NOT NULL,
    year INTEGER NOT NULL,
    n_rows INTEGER NOT NULL,
    complete BOOLEAN NOT NULL,            -- 年度已結束才算完整；當年需重抓
    fetched_at TIMESTAMP NOT NULL,
    PRIMARY KEY (source, year)
);
CREATE TABLE IF NOT EXISTS adj_factors (
    code VARCHAR NOT NULL,
    date DATE NOT NULL,                   -- 實際生效的交易日
    factor DOUBLE NOT NULL,               -- 當日事件的因子（參考價 ÷ 事件前收盤價）
    cum_factor DOUBLE NOT NULL,           -- 此日（含）之後所有事件因子的乘積
    PRIMARY KEY (code, date)
);
-- 季財報：合併損益與資產負債，附可取得日（法定期限次日；金融保險等格式期限較晚）。
-- 損益欄位為年初至今累計，單季數字見 financials_q。
CREATE OR REPLACE VIEW financials AS
WITH k AS (
    SELECT year, quarter, code FROM fin_income UNION SELECT year, quarter, code FROM fin_balance
)
SELECT k.year, k.quarter, k.code,
       coalesce(i.market, b.market) AS market, coalesce(i.name, b.name) AS name,
       CASE WHEN 'financial' IN (i.layout, b.layout) THEN 'financial' ELSE 'general' END AS layout,
       CASE WHEN k.quarter = 4 THEN make_date(k.year + 1, 4, 1)
            WHEN 'financial' IN (i.layout, b.layout)
                 THEN [make_date(k.year, 5, 31), make_date(k.year, 9, 1),
                       make_date(k.year, 11, 30)][k.quarter]
            ELSE [make_date(k.year, 5, 16), make_date(k.year, 8, 15), make_date(k.year, 11, 15)][k.quarter]
       END AS available_date,
       i.revenue, i.gross_profit, i.operating_income, i.pretax_income, i.net_income,
       coalesce(i.net_income_parent, i.net_income) AS net_income_parent, i.eps,
       b.current_assets, b.total_assets, b.current_liabilities, b.total_liabilities, b.share_capital,
       coalesce(b.equity_parent, b.equity) AS equity_parent, b.equity, b.bvps
FROM k LEFT JOIN fin_income i USING (year, quarter, code) LEFT JOIN fin_balance b USING (year, quarter, code);
-- 向後還原：最新價格不變，事件日之前的價格乘上之後所有事件的因子。
CREATE OR REPLACE VIEW daily_quotes_adj AS
SELECT q.*, coalesce(f.cum_factor, 1.0) AS adj_factor,
       q.open * coalesce(f.cum_factor, 1.0) AS adj_open,
       q.high * coalesce(f.cum_factor, 1.0) AS adj_high,
       q.low * coalesce(f.cum_factor, 1.0) AS adj_low,
       q.close * coalesce(f.cum_factor, 1.0) AS adj_close
FROM daily_quotes q ASOF LEFT JOIN adj_factors f ON q.code = f.code AND q.date < f.date;
"""

# 單季損益：累計數減去同年前一季累計（前一季缺漏時為 NULL）。
_Q_ITEMS = ["revenue", "gross_profit", "operating_income", "pretax_income", "net_income", "net_income_parent",
            "eps"]
FINANCIALS_Q_VIEW = """
CREATE OR REPLACE VIEW financials_q AS
SELECT * EXCLUDE (_prev_q) REPLACE ({})
FROM (SELECT f.*, lag(quarter) OVER (PARTITION BY code, year ORDER BY quarter) AS _prev_q FROM financials f)
WINDOW w AS (PARTITION BY code, year ORDER BY quarter)
""".format(", ".join(
    f"CASE WHEN quarter = 1 THEN {c} WHEN _prev_q = quarter - 1 THEN {c} - lag({c}) OVER w END AS {c}"
    for c in _Q_ITEMS))

MARKETS = ("TWSE", "TPEX")


def now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


class Database:
    def __init__(self, path: Path | str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path))
        self.con.execute(SCHEMA)
        self.con.execute(FINANCIALS_Q_VIEW)

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

    def replace_holidays(self, year: int, rows: list[tuple[dt.date, str, bool]]) -> None:
        ts = now()
        self.con.execute("BEGIN")
        try:
            self.con.execute("DELETE FROM market_holidays WHERE year(date) = ?", [year])
            if rows:
                self.con.executemany("INSERT INTO market_holidays VALUES (?, ?, ?, ?)",
                                     [(d, n, c, ts) for d, n, c in rows])
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def replace_index_month(self, index_code: str, year: int, month: int,
                            rows: list[tuple[dt.date, float | None, float | None, float | None, float | None]]
                            ) -> None:
        first = dt.date(year, month, 1)
        nxt = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        self.con.execute("BEGIN")
        try:
            self.con.execute("DELETE FROM index_daily WHERE index_code = ? AND date >= ? AND date < ?",
                             [index_code, first, nxt])
            if rows:
                self.con.executemany("INSERT INTO index_daily VALUES (?, ?, ?, ?, ?, ?)",
                                     [(d, index_code, o, h, lo, c) for d, o, h, lo, c in rows])
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def expected_trading_days(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """交易日：已同步的日曆用實際交易日；之後的日子以「平日且非公布休市日」推估。

        推估無法預知颱風等臨時休市，排程使用時應以當天實際行情為準。
        """
        days = self.trading_days(start, end)
        latest = (self.con.execute("SELECT max(date) FROM trading_days").fetchone()[0]
                  or start - dt.timedelta(days=1))
        closed = {r[0] for r in self.con.execute(
            "SELECT date FROM market_holidays WHERE closed AND date BETWEEN ? AND ?",
            [start, end]).fetchall()}
        d = max(start, latest + dt.timedelta(days=1))
        while d <= end:
            if d.weekday() < 5 and d not in closed:
                days.append(d)
            d += dt.timedelta(days=1)
        return days

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

    # --- 每日資料集（三大法人、融資融券） --------------------------------

    def replace_daily(self, table: str, date: dt.date, market: str, rows: list) -> None:
        """整日整市場替換。rows 為 dataclass，欄位名稱與資料表欄位相同。"""
        cols = list(rows[0].__dataclass_fields__) if rows else []
        df = pd.DataFrame({c: [getattr(r, c) for r in rows] for c in cols})
        for c in cols:
            if c not in ("date", "market", "code"):
                df[c] = pd.array(df[c], dtype="Int64")
        self.con.execute("BEGIN")
        try:
            self.con.execute(f"DELETE FROM {table} WHERE date = ? AND market = ?", [date, market])
            if rows:
                df["date"] = pd.Series([date] * len(rows), dtype="object")
                self.con.register("_daily_df", df)
                col_sql = ", ".join(f'"{c}"' for c in cols)
                select = ", ".join("CAST(date AS DATE)" if c == "date" else f'"{c}"' for c in cols)
                self.con.execute(
                    f"INSERT INTO {table} ({col_sql}, fetched_at) SELECT {select}, ? FROM _daily_df", [now()])
                self.con.unregister("_daily_df")
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def set_dataset_status(self, dataset: str, date: dt.date, market: str, status: str,
                           n_rows: int | None = None, message: str | None = None) -> None:
        self.con.execute("INSERT OR REPLACE INTO dataset_status VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [dataset, date, market, status, n_rows, message, now()])

    def dataset_ok_dates(self, dataset: str, market: str) -> set[dt.date]:
        return {r[0] for r in self.con.execute(
            "SELECT date FROM dataset_status WHERE dataset = ? AND market = ? AND status = 'ok'",
            [dataset, market]).fetchall()}

    # --- 月營收、財報 -----------------------------------------------------

    def complete_periods(self, source: str) -> set[str]:
        return {r[0] for r in self.con.execute(
            "SELECT period FROM period_sync WHERE source = ? AND complete", [source]).fetchall()}

    def _replace_period(self, table: str, where: str, params: list, df: pd.DataFrame, source: str,
                        period: str, complete: bool) -> None:
        self.con.execute("BEGIN")
        try:
            self.con.execute(f"DELETE FROM {table} WHERE source = ? AND {where}", [source, *params])
            if len(df):
                self.con.register("_period_df", df)
                cols = ", ".join(f'"{c}"' for c in df.columns)
                self.con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _period_df")
                self.con.unregister("_period_df")
            self.con.execute("INSERT OR REPLACE INTO period_sync VALUES (?, ?, ?, ?, ?)",
                             [source, period, len(df), complete, now()])
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def replace_revenue(self, source: str, period: str, year: int, month: int, rows: list[Revenue],
                        complete: bool) -> int:
        """整頁替換。同一公司若已由其他頁面寫入（國內／外國頁重複）則保留先寫入者，回傳略過筆數。"""
        existing = {r[0] for r in self.con.execute(
            "SELECT code FROM monthly_revenue WHERE year = ? AND month = ? AND source <> ?",
            [year, month, source]).fetchall()}
        keep = list({r.code: r for r in rows if r.code not in existing}.values())
        df = pd.DataFrame([vars(r) for r in keep], columns=list(Revenue.__dataclass_fields__))
        for c in ("revenue", "revenue_last_year", "cum_revenue", "cum_revenue_last_year"):
            df[c] = pd.array(df[c], dtype="Int64")
        df["available_date"] = pd.to_datetime(df["available_date"]).dt.date
        df["source"], df["fetched_at"] = source, now()
        self._replace_period("monthly_revenue", "year = ? AND month = ?", [year, month], df, source, period,
                             complete)
        return len(rows) - len(keep)

    def replace_financials(self, statement: str, source: str, market: str, year: int, quarter: int,
                           rows: list[FinRow], complete: bool) -> None:
        table = {"income": "fin_income", "balance": "fin_balance"}[statement]
        dedup = {r.code: r for r in rows}
        df = pd.DataFrame([{"year": year, "quarter": quarter, "code": r.code, "market": market,
                            "name": r.name, "layout": r.layout, **r.values} for r in dedup.values()])
        if len(df):
            df["source"], df["fetched_at"] = source, now()
        self._replace_period(table, "year = ? AND quarter = ?", [year, quarter], df, source,
                             f"{year}Q{quarter}", complete)

    def insert_supplement(self, table: str, source: str, code: str, df: pd.DataFrame) -> int:
        """補充來源（FinMind）：替換該代號舊的補充資料，主鍵已存在（觀測站已有）的列略過。回傳寫入筆數。"""
        self.con.execute("BEGIN")
        try:
            self.con.execute(f"DELETE FROM {table} WHERE source = ? AND code = ?", [source, code])
            before = self.con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0]
            if len(df):
                self.con.register("_supp_df", df)
                cols = ", ".join(f'"{c}"' for c in df.columns)
                self.con.execute(f"INSERT OR IGNORE INTO {table} ({cols}) SELECT {cols} FROM _supp_df")
                self.con.unregister("_supp_df")
            after = self.con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0]
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return after - before

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
        """由 ok 日的行情重建證券主檔，並合併最新 ISIN 一覽表與終止上市櫃清單。

        status：
        - delisted：最後交易日之後，在最後交易的市場有終止上市（櫃）紀錄，且一覽表沒有列在其他市場。
          一覽表可能晚一兩天才移除（1589 永冠-KY 終止上市當天仍在表上），所以終止紀錄優先。
        - listed：在最新一覽表中（含停止買賣、尚未終止者）。
        - ended：不在一覽表，也沒有終止紀錄（ETF 清算、ETN 到期等）。
        上櫃轉上市：終止上櫃日期不晚於上市後的最後交易日，且一覽表列在上市，所以不會被當成下市。
        """
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
                    (code, name, market, sec_type, first_date, last_date, n_days,
                     status, delisted_date, industry, listed_date, board)
                WITH q AS (
                    SELECT code,
                           arg_max(name, date) AS name, arg_max(market, date) AS market,
                           arg_max(sec_type, date) AS sec_type,
                           min(date) AS first_date, max(date) AS last_date, count(*) AS n_days
                    FROM ok_quotes GROUP BY code
                ),
                snap AS (
                    SELECT * FROM listing_snapshots
                    WHERE month = (SELECT max(month) FROM listing_snapshots)
                ),
                dl AS (  -- 最後交易日之後、同市場的第一筆終止紀錄（早於首次交易的舊代號紀錄不會符合）
                    SELECT q.code, min(d.date) AS date
                    FROM q JOIN delistings d
                      ON d.code = q.code AND d.market = q.market AND d.date > q.last_date
                    GROUP BY q.code
                ),
                rev AS (  -- 一覽表沒有的（已下市櫃）改用最後一次月營收公告的產業別
                    SELECT code, arg_max(industry, year * 100 + month) AS industry
                    FROM monthly_revenue WHERE industry IS NOT NULL GROUP BY code
                )
                SELECT q.code, q.name, q.market, q.sec_type, q.first_date, q.last_date, q.n_days,
                       CASE WHEN dl.code IS NOT NULL AND coalesce(snap.market, q.market) = q.market
                                 THEN 'delisted'
                            WHEN snap.code IS NOT NULL THEN 'listed' ELSE 'ended' END,
                       CASE WHEN dl.code IS NOT NULL AND coalesce(snap.market, q.market) = q.market
                                 THEN dl.date END,
                       coalesce(snap.industry, rev.industry, fb.industry), snap.listed_date, snap.board
                FROM q LEFT JOIN snap USING (code) LEFT JOIN dl USING (code) LEFT JOIN rev USING (code)
                LEFT JOIN industry_fallback fb USING (code)
            """)
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return self.con.execute("SELECT count(*) FROM securities").fetchone()[0]

    # --- 掛牌資訊與終止上市櫃 -----------------------------------------------

    def replace_listing_snapshot(self, month: str, listings: list[Listing]) -> None:
        """替換該月份、這批資料所屬市場的一覽表。"""
        df = pd.DataFrame([vars(x) for x in listings])
        df["listed_date"] = pd.Series(list(df["listed_date"]), dtype="object")
        markets = sorted({x.market for x in listings})
        self.con.execute("BEGIN")
        try:
            self.con.execute("DELETE FROM listing_snapshots WHERE month = ? AND list_contains(?, market)",
                             [month, markets])
            self.con.register("_listings_df", df)
            self.con.execute("""
                INSERT INTO listing_snapshots
                SELECT ?, code, name, market, board, section, industry, CAST(listed_date AS DATE),
                       isin, cfi, ? FROM _listings_df
            """, [month, now()])
            self.con.unregister("_listings_df")
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def replace_delistings(self, source: str, rows: list[Delisting], year: int | None = None,
                           complete: bool = False) -> None:
        """整來源替換；year 有值時只替換該年度並記錄同步狀態（逐年查詢的來源）。"""
        dedup = {(r.date, r.market, r.code): r for r in rows}
        ts = now()
        self.con.execute("BEGIN")
        try:
            if year is None:
                self.con.execute("DELETE FROM delistings WHERE source = ?", [source])
            else:
                self.con.execute("DELETE FROM delistings WHERE source = ? AND year(date) = ?", [source, year])
                self.con.execute("INSERT OR REPLACE INTO action_sync VALUES (?, ?, ?, ?, ?)",
                                 [source, year, len(dedup), complete, ts])
            if dedup:
                self.con.executemany("INSERT INTO delistings VALUES (?, ?, ?, ?, ?, ?, ?)",
                                     [(r.date, r.market, r.code, r.name, r.reason, source, ts)
                                      for r in dedup.values()])
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    # --- 公司行動與還原因子 -------------------------------------------------

    def replace_actions(self, source: str, market: str, kind: str, year: int, actions: list[Action],
                        complete: bool) -> int:
        """整年整來源替換。同日同代號重複時保留最後一筆，回傳被合併的筆數。"""
        dedup = {(a.date, a.code): a for a in actions}
        ts = now()
        self.con.execute("BEGIN")
        try:
            self.con.execute("DELETE FROM corporate_actions WHERE source = ? AND year(date) = ?",
                             [source, year])
            if dedup:
                self.con.executemany(
                    "INSERT INTO corporate_actions (date, market, code, kind, name, prev_close, ref_price,"
                    " theo_ref, note, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(a.date, a.market, a.code, a.kind, a.name, a.prev_close, a.ref_price, a.theo_ref, a.note,
                      source, ts) for a in dedup.values()])
            self.con.execute("INSERT OR REPLACE INTO action_sync VALUES (?, ?, ?, ?, ?)",
                             [source, year, len(dedup), complete, ts])
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return len(actions) - len(dedup)

    def complete_action_years(self, source: str) -> set[int]:
        """已完整同步的年度（公司行動與逐年查詢的終止上櫃共用 action_sync）。"""
        return {r[0] for r in self.con.execute(
            "SELECT year FROM action_sync WHERE source = ? AND complete", [source]).fetchall()}

    def refresh_adjustments(self) -> int:
        """由公司行動重建還原因子。只採用已有行情的日期，未來事件不影響目前價格。"""
        self.con.execute("BEGIN")
        try:
            self.con.execute("DELETE FROM adj_factors")
            self.con.execute("""
                INSERT INTO adj_factors
                WITH ev AS (
                    SELECT code, eff_date AS date, exp(sum(ln(ref_price / prev_close))) AS factor
                    FROM corporate_actions_eff
                    WHERE prev_close > 0 AND ref_price > 0
                      AND eff_date <= (SELECT max(date) FROM daily_quotes)
                    GROUP BY code, eff_date
                )
                SELECT code, date, factor,
                       exp(sum(ln(factor)) OVER (PARTITION BY code ORDER BY date DESC
                                                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW))
                FROM ev
            """)
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return self.con.execute("SELECT count(*) FROM adj_factors").fetchone()[0]
