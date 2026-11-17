"""擷取流程：交易日曆同步、單日擷取、回補、每日更新。"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from apex import quality
from apex.db import MARKETS, Database
from apex.http import HttpClient
from apex.raw_store import RawStore, sha256
from apex.sources import NotPublished, ParseError, tpex, twse

log = logging.getLogger(__name__)

# (來源名稱, 鍵) → 回應文字。測試時可替換成讀取樣本檔。
Fetcher = Callable[[str, str], str]

QUOTE_SOURCES = {"TWSE": ("twse_quotes", twse.parse_quotes), "TPEX": ("tpex_quotes", tpex.parse_quotes)}
CALENDAR_SOURCE = "twse_taiex"


def http_fetcher(client: HttpClient) -> Fetcher:
    def fetch(source: str, key: str) -> str:
        if source == "twse_quotes":
            return client.get_text(twse.QUOTES_URL, twse.quotes_params(_key_date(key)))
        if source == "tpex_quotes":
            return client.get_text(tpex.QUOTES_URL, tpex.quotes_params(_key_date(key)))
        if source == CALENDAR_SOURCE:
            return client.get_text(twse.TAIEX_MONTH_URL, twse.taiex_month_params(int(key[:4]), int(key[4:6])))
        raise ValueError(f"未知來源：{source}")
    return fetch


def _key_date(key: str) -> dt.date:
    return dt.datetime.strptime(key, "%Y%m%d").date()


def month_range(start: dt.date, end: dt.date) -> list[tuple[int, int]]:
    months, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


@dataclass
class DayResult:
    date: dt.date
    status: dict[str, str] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(self.status.get(m) == "ok" for m in MARKETS)


class Pipeline:
    def __init__(self, db: Database, raw: RawStore, fetcher: Fetcher):
        self.db = db
        self.raw = raw
        self.fetcher = fetcher

    def _get(self, source: str, key: str, refetch: bool) -> str:
        if not refetch and self.raw.exists(source, key):
            return self.raw.load(source, key)
        text = self.fetcher(source, key)
        self.raw.save(source, key, text)
        return text

    # --- 交易日曆 -----------------------------------------------------------

    def sync_calendar(self, start: dt.date, end: dt.date, today: dt.date | None = None) -> int:
        """同步 start～end 所在月份的交易日。已完整的月份略過；當月一律重抓。"""
        today = today or dt.date.today()
        done = self.db.complete_months()
        n_fetched = 0
        for year, month in month_range(start, min(end, today)):
            label = f"{year:04d}-{month:02d}"
            if label in done:
                continue
            key = f"{year:04d}{month:02d}"
            next_month = (dt.date(year, month, 28) + dt.timedelta(days=4)).replace(day=1)
            complete = next_month <= today
            text = self._get(CALENDAR_SOURCE, key, refetch=not complete)
            try:
                days = [d["date"] for d in twse.parse_taiex_month(text)]
            except NotPublished:
                # 例如當月第一個交易日之前
                self.raw.delete(CALENDAR_SOURCE, key)
                days = []
            if any(d.year != year or d.month != month for d in days):
                raise ParseError(f"{label} 交易日曆含有其他月份日期")
            self.db.save_calendar_month(year, month, days, complete)
            n_fetched += 1
            log.info("交易日曆 %s：%d 天%s", label, len(days), "" if complete else "（當月，未完整）")
        return n_fetched

    # --- 單日擷取 -----------------------------------------------------------

    def ingest_date(self, date: dt.date, refetch: bool = False) -> DayResult:
        result = DayResult(date)
        key = date.strftime("%Y%m%d")
        for market, (source, parse) in QUOTE_SOURCES.items():
            text = None
            try:
                text = self._get(source, key, refetch)
                quotes = parse(text, date)
            except NotPublished as e:
                # 尚未發布：刪除原始檔，下次重新抓
                self.raw.delete(source, key)
                self.db.set_status(date, market, "failed", 0, f"尚未發布：{e}")
                result.status[market] = "failed"
                result.messages.append(f"{market} 尚未發布")
                continue
            except ParseError as e:
                # 格式錯誤：保留原始檔供人工檢查
                self.db.set_status(date, market, "failed", None, f"解析失敗：{e}",
                                   sha256(text) if text else None)
                result.status[market] = "failed"
                result.messages.append(f"{market} 解析失敗：{e}")
                continue
            self.db.replace_quotes(date, market, quotes)
            self.db.set_status(date, market, "ok", len(quotes), None, sha256(text))
            result.status[market] = "ok"
            result.rows[market] = len(quotes)

        dq = quality.check_date(self.db, date)
        self.db.save_dq(date, dq)
        for market in MARKETS:
            errors = quality.has_error(dq, market)
            if errors and result.status.get(market) == "ok":
                detail = "；".join(f"{c}：{d}" for _, c, _, d in errors)
                self.db.set_status(date, market, "failed", result.rows.get(market), f"品質檢查失敗：{detail}")
                result.status[market] = "failed"
                result.messages.append(f"{market} 品質檢查失敗：{detail}")
        warnings = [f"{m} {c}" for m, c, s, _ in dq if s == "warning"]
        if warnings:
            result.messages.append("警告：" + "、".join(warnings))
        return result

    # --- 回補與每日更新 -----------------------------------------------------

    def backfill(self, start: dt.date, end: dt.date, refetch: bool = False,
                 today: dt.date | None = None, reprocess: bool = False) -> list[DayResult]:
        """refetch：重新下載；reprocess：已成功的日子也用原始檔重新解析（規則修改後使用）。"""
        today = today or dt.date.today()
        self.sync_calendar(start, end, today)
        days = self.db.trading_days(start, min(end, today))
        results = []
        for i, date in enumerate(days, 1):
            if not (refetch or reprocess) and all(self.db.status(date, m) == "ok" for m in MARKETS):
                continue
            r = self.ingest_date(date, refetch)
            results.append(r)
            rows = "、".join(f"{m} {n}" for m, n in r.rows.items())
            log.info("[%d/%d] %s %s %s %s", i, len(days), date, "OK" if r.ok else "FAILED", rows,
                     "；".join(r.messages))
            if i % 50 == 0:
                self.db.refresh_securities()
        self.db.refresh_securities()
        return results

    def update(self, today: dt.date | None = None, lookback_days: int = 31) -> list[DayResult]:
        """每日更新：同步近期交易日曆，補齊近 lookback_days 天內所有未成功的交易日。"""
        today = today or dt.date.today()
        return self.backfill(today - dt.timedelta(days=lookback_days), today, today=today)
