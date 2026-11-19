"""回測資料：從資料庫載入行情面板，並提供「只看得到當時資料」的存取介面。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from apex.db import Database

PANEL_FIELDS = ["open", "close", "adj_open", "adj_close", "volume", "value"]


@dataclass
class MarketData:
    """日行情面板：index 為交易日，columns 為代號。沒有成交時價格為 NaN。"""

    days: pd.DatetimeIndex
    panels: dict[str, pd.DataFrame]
    securities: pd.DataFrame  # index 為代號：sec_type、status、last_date、delisted_date、industry
    db: Database

    def __getitem__(self, field: str) -> pd.DataFrame:
        return self.panels[field]


def load(db: Database, start: dt.date, end: dt.date, sec_types: tuple[str, ...] = ("stock", "etf"),
         warmup_days: int = 300) -> MarketData:
    """載入 start 前 warmup_days 個交易日起的行情（供策略計算指標），只含 ok 的交易日。"""
    first = db.con.execute(
        "SELECT min(date) FROM (SELECT date FROM trading_days WHERE date < ? ORDER BY date DESC LIMIT ?)",
        [start, warmup_days]).fetchone()[0] or start
    df = db.con.execute(f"""
        SELECT q.date, q.code, q.open, q.close, q.adj_open, q.adj_close, q.volume::DOUBLE AS volume,
               q.value::DOUBLE AS value
        FROM daily_quotes_adj q
        WHERE q.date BETWEEN ? AND ? AND q.sec_type IN ({",".join("?" * len(sec_types))})
          AND q.date IN (SELECT date FROM ingest_status GROUP BY date HAVING bool_and(status = 'ok'))
    """, [first, end, *sec_types]).df()
    df["date"] = pd.to_datetime(df["date"])
    panels = {f: df.pivot(index="date", columns="code", values=f).sort_index() for f in PANEL_FIELDS}
    days = panels["close"].index
    for f in PANEL_FIELDS:
        panels[f] = panels[f].reindex(index=days)
    sec = db.con.execute("""
        SELECT code, sec_type, status, last_date, delisted_date, industry, name FROM securities
    """).df().set_index("code")
    for col in ("last_date", "delisted_date"):  # 引擎以 datetime.date 比較
        sec[col] = [None if pd.isna(v) else pd.Timestamp(v).date() for v in sec[col]]
    return MarketData(days, panels, sec, db)


class PointInTime:
    """基本面資料的時點查詢：只回傳 available_date ≤ 查詢日的資料，避免前視偏誤。"""

    def __init__(self, db: Database, end: dt.date):
        self.revenue = db.con.execute("""
            SELECT code, year, month, revenue, revenue_last_year, cum_revenue, cum_revenue_last_year,
                   available_date
            FROM monthly_revenue WHERE available_date <= ? ORDER BY available_date
        """, [end]).df()
        self.financials = db.con.execute("""
            SELECT * FROM financials_q WHERE available_date <= ? ORDER BY available_date
        """, [end]).df()
        for df in (self.revenue, self.financials):
            df["available_date"] = pd.to_datetime(df["available_date"])

    @staticmethod
    def _latest(df: pd.DataFrame, date: pd.Timestamp, n: int) -> pd.DataFrame:
        visible = df[df["available_date"] <= date]
        return visible.groupby("code", sort=False).tail(n)

    def latest_revenue(self, date: pd.Timestamp, n: int = 1) -> pd.DataFrame:
        """每家公司在 date 當天已公告的最近 n 個月營收。"""
        return self._latest(self.revenue, date, n)

    def latest_financials(self, date: pd.Timestamp, n: int = 1) -> pd.DataFrame:
        """每家公司在 date 當天已公告的最近 n 季財報（單季損益、季底資產負債）。"""
        return self._latest(self.financials, date, n)
