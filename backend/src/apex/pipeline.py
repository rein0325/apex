"""擷取流程：交易日曆同步、單日擷取、回補、每日更新。"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from apex import quality
from apex.db import MARKETS, Database
from apex.http import HttpClient
from apex.raw_store import RawStore, sha256
from apex.sources import NotPublished, ParseError, actions, chips, finmind, fundamentals, listings, tpex, twse

log = logging.getLogger(__name__)

# (來源名稱, 鍵) → 回應文字。測試時可替換成讀取樣本檔。
Fetcher = Callable[[str, str], str]

QUOTE_SOURCES = {"TWSE": ("twse_quotes", twse.parse_quotes), "TPEX": ("tpex_quotes", tpex.parse_quotes)}
CALENDAR_SOURCE = "twse_taiex"
TAIEX_TR_SOURCE = "twse_taiex_tr"  # 鍵：YYYYMM
HOLIDAY_SOURCE = "twse_holidays"  # 鍵：年度
ISIN_SOURCES = {"TWSE": "twse_isin", "TPEX": "tpex_isin"}  # 鍵：YYYYMM（每月一份）
TWSE_DELISTED = "twse_delisted"  # 鍵：擷取日 YYYYMMDD（一次回傳全部年度）
TPEX_DELISTED = "tpex_delisted"  # 鍵：年度
DELIST_START_YEAR = 2011  # 2026-11-22 回補 2011～2014（docs/研究報告/大漲股研究.md 早期考試）

# 每日資料集：資料表 → {市場: (來源名稱, 網址, 參數函式, 解析函式)}。鍵為 YYYYMMDD。
DAILY_DATASETS = {
    "institutional": {
        "TWSE": ("twse_insti", chips.TWSE_INSTI_URL, chips.twse_insti_params, chips.parse_twse_insti),
        "TPEX": ("tpex_insti", chips.TPEX_INSTI_URL, chips.tpex_insti_params, chips.parse_tpex_insti),
    },
    "margin": {
        "TWSE": ("twse_margin", chips.TWSE_MARGIN_URL, chips.twse_margin_params, chips.parse_twse_margin),
        "TPEX": ("tpex_margin", chips.TPEX_MARGIN_URL, chips.tpex_margin_params, chips.parse_tpex_margin),
    },
}
DAILY_SOURCES = {src: (url, params) for ds in DAILY_DATASETS.values() for src, url, params, _ in ds.values()}

# 月營收：(市場, 是否外國公司頁) → 來源，鍵 YYYYMM。財報：(報表, 市場) → 來源，鍵 YYYYQn。
REVENUE_SOURCES = {(m, f): f"mops_revenue_{fundamentals.MARKET_TYPEK[m]}_{int(f)}"
                   for m in MARKETS for f in (False, True)}
FIN_SOURCES = {(s, m): f"mops_{s}_{fundamentals.MARKET_TYPEK[m]}"
               for s in fundamentals.FIN_URLS for m in MARKETS}
FUNDAMENTAL_START_YEAR = 2015
IFRS_START_YEAR = 2013  # 觀測站彙總財報從 2013（IFRS）起；更早是舊格式，不抓
FINMIND_START = "2011"  # FinMind 補下市公司資料的起始年；原始檔鍵為「代號@起始年」
COMPLETE_AFTER_DAYS = 30  # 公告期限過後 30 天視為完整（延遲申報、更正）


def http_fetcher(client: HttpClient) -> Fetcher:
    finmind_client = HttpClient(finmind.DELAY_SEC, client.max_retries, client.timeout_sec)

    def fetch(source: str, key: str) -> str:
        if source.startswith("finmind_"):
            code, _, since = key.partition("@")
            start = f"{since}-01-01" if since else "2015-01-01"
            return finmind_client.get_text(finmind.API,
                                           finmind.params(source.removeprefix("finmind_"), code, start))
        if source == "twse_quotes":
            return client.get_text(twse.QUOTES_URL, twse.quotes_params(_key_date(key)))
        if source == "tpex_quotes":
            return client.get_text(tpex.QUOTES_URL, tpex.quotes_params(_key_date(key)))
        if source in actions.SPEC_BY_SOURCE:
            spec, year = actions.SPEC_BY_SOURCE[source], int(key)
            return client.get_text(spec.url, spec.params(dt.date(year, 1, 1), dt.date(year, 12, 31)))
        if source in DAILY_SOURCES:
            url, params = DAILY_SOURCES[source]
            return client.get_text(url, params(_key_date(key)))
        for (market, foreign), src in REVENUE_SOURCES.items():
            if source == src:
                url = fundamentals.revenue_url(market, int(key[:4]), int(key[4:6]), foreign)
                return client.get_text(url, encoding=fundamentals.MOPS_ENCODING_REVENUE)
        for (statement, market), src in FIN_SOURCES.items():
            if source == src:
                return client.post_text(fundamentals.FIN_URLS[statement],
                                        fundamentals.fin_form(market, int(key[:4]), int(key[5])))
        if source in ISIN_SOURCES.values():
            market = next(m for m, s in ISIN_SOURCES.items() if s == source)
            return client.get_text(listings.ISIN_URL, {"strMode": listings.ISIN_MODES[market]},
                                   encoding=listings.ISIN_ENCODING)
        if source == TWSE_DELISTED:
            return client.get_text(listings.TWSE_DELISTED_URL, {"response": "json"})
        if source == TPEX_DELISTED:
            return client.get_text(listings.TPEX_DELISTED_URL, {"date": key, "response": "json"})
        if source == TAIEX_TR_SOURCE:
            return client.get_text(twse.TAIEX_TR_URL, twse.taiex_month_params(int(key[:4]), int(key[4:6])))
        if source == HOLIDAY_SOURCE:
            return client.get_text(twse.HOLIDAY_URL, twse.holiday_params(int(key)))
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

    def sync_holidays(self, today: dt.date | None = None) -> int:
        """同步今年與明年的開休市日期（每次重抓；明年尚未公布時略過）。"""
        today = today or dt.date.today()
        n = 0
        for year in (today.year, today.year + 1):
            try:
                rows = twse.parse_holidays(self._get(HOLIDAY_SOURCE, str(year), refetch=True), year)
            except NotPublished as e:
                self.raw.delete(HOLIDAY_SOURCE, str(year))
                log.info("%s", e)
                continue
            self.db.replace_holidays(year, rows)
            n += len(rows)
            log.info("休市日期 %d：%d 筆（休市 %d 天）", year, len(rows), sum(c for _, _, c in rows))
        return n

    def sync_indices(self, start: dt.date, end: dt.date, today: dt.date | None = None) -> int:
        """市場指數：加權指數取自交易日曆的原始檔；報酬指數逐月下載（當月一律重抓）。"""
        today = today or dt.date.today()
        n = 0
        for year, month in month_range(start, min(end, today)):
            key = f"{year:04d}{month:02d}"
            current = (year, month) == (today.year, today.month)
            try:
                taiex = twse.parse_taiex_month(self._get(CALENDAR_SOURCE, key, refetch=current))
                self.db.replace_index_month("TAIEX", year, month, [
                    (r["date"], r["open"], r["high"], r["low"], r["close"]) for r in taiex])
                tr = twse.parse_taiex_tr_month(self._get(TAIEX_TR_SOURCE, key, refetch=current))
                self.db.replace_index_month("TAIEX_TR", year, month,
                                            [(d, None, None, None, c) for d, c in tr])
            except NotPublished as e:
                self.raw.delete(TAIEX_TR_SOURCE, key)
                log.info("指數 %s：%s", key, e)
                continue
            n += len(taiex)
        return n

    # --- 公司行動 -----------------------------------------------------------

    def sync_actions(self, start_year: int, end_year: int, today: dt.date | None = None,
                     refetch: bool = False, reprocess: bool = False) -> int:
        """同步除權息、減資、變更面額（逐年整批查詢）。已完整的年度略過；當年一律重抓。

        reprocess：已完整的年度也用原始檔重新解析（規則修改後使用）。
        """
        today = today or dt.date.today()
        total = 0
        for spec in actions.SPECS:
            done = self.db.complete_action_years(spec.source)
            for year in range(start_year, min(end_year, today.year) + 1):
                if year in done and not (refetch or reprocess):
                    continue
                complete = dt.date(year, 12, 31) < today
                key = str(year)
                text = self._get(spec.source, key, refetch=refetch or not complete)
                rows = actions.parse(spec, text)
                merged = self.db.replace_actions(spec.source, spec.market, spec.kind, year, rows, complete)
                total += len(rows)
                log.info("%s %d：%d 筆%s%s", spec.source, year, len(rows),
                         f"（{merged} 筆同日重複已合併）" if merged else "",
                         "" if complete else "（當年，未完整）")
        n = self.db.refresh_adjustments()
        log.info("還原因子：%d 個事件", n)
        return total

    # --- 每日資料集（三大法人、融資融券） --------------------------------

    def sync_daily(self, start: dt.date, end: dt.date, datasets: list[str] | None = None,
                   refetch: bool = False, reprocess: bool = False) -> dict[str, int]:
        """擷取日行情為 ok 的交易日的籌碼資料。已成功的日子略過（可中斷續跑）。

        同一天依序請求證交所、櫃買中心，兩個主機的限速間隔可以重疊。
        """
        datasets = datasets or list(DAILY_DATASETS)
        days = [d for d in self.db.trading_days(start, end)
                if all(self.db.status(d, m) == "ok" for m in MARKETS)]
        done = {(ds, m): self.db.dataset_ok_dates(ds, m) for ds in datasets for m in MARKETS}
        counts = {"ok": 0, "failed": 0}
        for i, date in enumerate(days, 1):
            key = date.strftime("%Y%m%d")
            msgs = []
            for ds in datasets:
                for market, (source, _, _, parse) in DAILY_DATASETS[ds].items():
                    if date in done[(ds, market)] and not (refetch or reprocess):
                        continue
                    try:
                        rows = parse(self._get(source, key, refetch), date)
                        if not rows:
                            raise NotPublished("0 筆")
                    except NotPublished as e:
                        self.raw.delete(source, key)
                        self.db.set_dataset_status(ds, date, market, "failed", 0, f"尚未發布：{e}")
                        counts["failed"] += 1
                        msgs.append(f"{ds} {market} 尚未發布")
                        continue
                    except ParseError as e:
                        self.db.set_dataset_status(ds, date, market, "failed", None, f"解析失敗：{e}")
                        counts["failed"] += 1
                        msgs.append(f"{ds} {market} 解析失敗：{e}")
                        continue
                    self.db.replace_daily(ds, date, market, rows)
                    self.db.set_dataset_status(ds, date, market, "ok", len(rows))
                    counts["ok"] += 1
                    msgs.append(f"{ds} {market} {len(rows)}")
            if msgs:
                log.info("[%d/%d] %s %s", i, len(days), date, "、".join(msgs))
        return counts

    # --- 月營收、財報 -----------------------------------------------------

    def sync_fundamentals(self, start_year: int = FUNDAMENTAL_START_YEAR, today: dt.date | None = None,
                          refetch: bool = False, reprocess: bool = False) -> dict[str, int]:
        """同步月營收與季財報。已完整的期間略過；公告期限後 30 天內的期間每次重抓。"""
        today = today or dt.date.today()
        counts = {"revenue": 0, "financials": 0, "pending": 0}

        def run(source: str, key: str, complete: bool, done: set[str], period: str, parse, save) -> None:
            if period in done and not (refetch or reprocess):
                return
            try:
                rows = parse(self._get(source, key, refetch or not complete))
            except NotPublished as e:
                self.raw.delete(source, key)
                counts["pending"] += 1
                log.info("%s %s：%s", source, key, e)
                return
            save(rows)
            log.info("%s %s：%d 筆%s", source, key, len(rows), "" if complete else "（未完整）")

        for year, month in month_range(dt.date(start_year, 1, 1), today):
            available = fundamentals.revenue_available(year, month)
            if dt.date(year, month, 1) >= today.replace(day=1):
                continue  # 當月尚未結束
            complete = today >= available + dt.timedelta(days=COMPLETE_AFTER_DAYS)
            for (market, _), source in REVENUE_SOURCES.items():
                key = f"{year}{month:02d}"

                def save(rows, source=source, key=key, year=year, month=month, complete=complete):
                    self.db.replace_revenue(source, key, year, month, rows, complete)
                    counts["revenue"] += len(rows)

                run(source, key, complete, self.db.complete_periods(source), key,
                    lambda t, m=market, y=year, mo=month: fundamentals.parse_revenue(t, m, y, mo), save)

        for year in range(max(start_year, IFRS_START_YEAR), today.year + 1):
            for quarter in range(1, 5):
                quarter_end = dt.date(year, quarter * 3, 1)
                if quarter_end >= today.replace(day=1):
                    continue
                available = fundamentals.fin_available(year, quarter, financial=True)
                complete = today >= available + dt.timedelta(days=COMPLETE_AFTER_DAYS)
                for (statement, market), source in FIN_SOURCES.items():
                    key = f"{year}Q{quarter}"

                    def save(rows, statement=statement, source=source, market=market, year=year,
                             quarter=quarter, complete=complete):
                        self.db.replace_financials(statement, source, market, year, quarter, rows, complete)
                        counts["financials"] += len(rows)

                    run(source, key, complete, self.db.complete_periods(source), key,
                        lambda t, s=statement: fundamentals.parse_financials(t, s), save)
        return counts

    def sync_finmind(self, refetch: bool = False) -> dict[str, int]:
        """以 FinMind 補齊已下市櫃普通股的月營收與財報（觀測站彙總表只含目前上市櫃公司）。

        原始檔以「代號@FINMIND_START」為鍵（2026-11-22 起從 2011 年抓起，舊的 2015 年起原始檔不再使用）；
        已下市的公司資料不會再變動，所以已下載的不重抓。額度用完時停止，下次續跑。
        """
        codes = self.db.con.execute("""
            SELECT code, market, name FROM securities
            WHERE sec_type = 'stock' AND status <> 'listed' ORDER BY code
        """).fetchall()
        counts = {"revenue": 0, "income": 0, "balance": 0, "industry": 0}
        official = {r[0] for r in self.db.con.execute(
            "SELECT DISTINCT industry FROM listing_snapshots WHERE industry IS NOT NULL").fetchall()}
        if official:
            key = dt.date.today().strftime("%Y%m")
            industries = finmind.parse_industries(self._get("finmind_info", key, refetch), official)
            self.db.con.execute("DELETE FROM industry_fallback")
            self.db.con.executemany("INSERT INTO industry_fallback VALUES (?, ?, 'finmind')",
                                    sorted(industries.items()))
            counts["industry"] = len(industries)
        done = {r[0] for r in self.db.con.execute(
            "SELECT code FROM finmind_sync WHERE since = ?", [FINMIND_START]).fetchall()}
        for n, (code, market, name) in enumerate(codes, 1):
            if code in done and not refetch:
                continue
            for ds in ("revenue", "income", "balance"):
                source = f"finmind_{ds}"
                try:
                    text = self._get(source, f"{code}@{FINMIND_START}", refetch)
                    if ds == "revenue":
                        rows = finmind.parse_revenue(text, code, market, name)
                        df = pd.DataFrame([vars(r) for r in rows],
                                          columns=list(fundamentals.Revenue.__dataclass_fields__))
                        table = "monthly_revenue"
                    else:
                        parse = finmind.parse_income if ds == "income" else finmind.parse_balance
                        df = pd.DataFrame([{"year": y, "quarter": q, "code": code, "market": market,
                                            "name": name, "layout": r.layout, **r.values}
                                           for (y, q), r in parse(text, code, name).items()])
                        table = "fin_income" if ds == "income" else "fin_balance"
                except NotPublished as e:
                    self.raw.delete(source, f"{code}@{FINMIND_START}")
                    log.warning("%s，已處理 %d/%d 檔，下次續跑", e, n - 1, len(codes))
                    return counts
                if len(df):
                    df["source"], df["fetched_at"] = "finmind", dt.datetime.now().replace(microsecond=0)
                counts[ds] += self.db.insert_supplement(table, "finmind", code, df)
            self.db.con.execute("INSERT OR REPLACE INTO finmind_sync VALUES (?, ?, ?)",
                                [code, FINMIND_START, dt.datetime.now().replace(microsecond=0)])
            log.info("[%d/%d] FinMind %s %s", n, len(codes), code, name)
        return counts

    # --- 掛牌資訊與終止上市櫃 ---------------------------------------------

    def sync_listings(self, today: dt.date | None = None, refetch: bool = False) -> int:
        """同步 ISIN 一覽表（產業別、上市櫃日）與終止上市櫃清單，並重建證券主檔。

        一覽表每月保存一份（上市含權證約 10 MB），refetch 時以最新資料覆蓋當月。
        終止上市清單很小，每天抓一次；終止上櫃逐年查詢，已結束的年度不重抓。
        """
        today = today or dt.date.today()
        month = today.strftime("%Y%m")
        for market, source in ISIN_SOURCES.items():
            rows = listings.parse_isin(self._get(source, month, refetch), market)
            self.db.replace_listing_snapshot(f"{month[:4]}-{month[4:]}", rows)
            log.info("ISIN 一覽表 %s %s：%d 筆", market, month, len(rows))

        rows = listings.parse_twse_delisted(self._get(TWSE_DELISTED, today.strftime("%Y%m%d"), refetch))
        self.db.replace_delistings(TWSE_DELISTED, rows)
        log.info("終止上市：%d 筆（%s～%s）", len(rows), min(r.date for r in rows), max(r.date for r in rows))

        done = self.db.complete_action_years(TPEX_DELISTED)
        for year in range(DELIST_START_YEAR, today.year + 1):
            if year in done and not refetch:
                continue
            complete = dt.date(year, 12, 31) < today
            rows = listings.parse_tpex_delisted(self._get(TPEX_DELISTED, str(year), refetch or not complete))
            if any(r.date.year != year for r in rows):
                raise ParseError(f"終止上櫃 {year} 含有其他年度資料")
            self.db.replace_delistings(TPEX_DELISTED, rows, year, complete)
            log.info("終止上櫃 %d：%d 筆%s", year, len(rows), "" if complete else "（當年，未完整）")
        return self.db.refresh_securities()

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
        start = today - dt.timedelta(days=lookback_days)
        # 先同步公司行動，品質檢查才能辨識除權息造成的價格跳動
        self.sync_actions(start.year, today.year, today)
        results = self.backfill(start, today, today=today)
        self.sync_daily(start, today)
        self.sync_listings(today)
        self.sync_fundamentals(today.year - 1, today)
        self.sync_indices(start, today, today)
        self.sync_holidays(today)
        self.db.refresh_adjustments()
        return results

    def recheck(self, start: dt.date, end: dt.date) -> int:
        """只重跑品質檢查（不重新解析），用於檢查規則或公司行動更新後。不改變 ok／failed。"""
        days = [d for d in self.db.trading_days(start, end)
                if any(self.db.status(d, m) is not None for m in MARKETS)]
        for date in days:
            self.db.save_dq(date, quality.check_date(self.db, date))
        return len(days)
