import datetime as dt
from pathlib import Path

import pytest

from apex import quality
from apex.db import Database
from apex.pipeline import Pipeline
from apex.raw_store import RawStore
from apex.sources import Quote

FIX = Path(__file__).parent / "fixtures"
D = dt.date(2026, 11, 13)

FIXTURE_MAP = {
    ("twse_taiex", "202611"): "twse_taiex_202611.json",
    ("twse_quotes", "20261113"): "twse_quotes_20261113.json",
    ("tpex_quotes", "20261113"): "tpex_quotes_20261113.json",
}


class FakeFetcher:
    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}

    def __call__(self, source, key):
        self.calls.append((source, key))
        if (source, key) in self.overrides:
            return self.overrides[(source, key)]
        name = FIXTURE_MAP.get((source, key))
        if name:
            return (FIX / name).read_text(encoding="utf-8")
        # 其他日期：模擬「尚未發布」
        if source == "tpex_quotes":
            return (FIX / "tpex_quotes_20261115_holiday.json").read_text(encoding="utf-8")
        return (FIX / "twse_quotes_20261115_holiday.json").read_text(encoding="utf-8")


@pytest.fixture
def pipe(tmp_path):
    db = Database()
    yield Pipeline(db, RawStore(tmp_path / "raw"), FakeFetcher())
    db.close()


def test_sync_calendar_current_month_not_complete(pipe):
    pipe.sync_calendar(dt.date(2026, 11, 1), dt.date(2026, 11, 30), today=dt.date(2026, 11, 14))
    days = pipe.db.trading_days(dt.date(2026, 11, 1), dt.date(2026, 11, 30))
    assert len(days) == 10 and D in days
    assert "2026-11" not in pipe.db.complete_months()


def test_ingest_date_ok_and_idempotent(pipe):
    pipe.sync_calendar(D, D, today=dt.date(2026, 11, 14))
    r1 = pipe.ingest_date(D)
    assert r1.ok, r1.messages
    n1 = pipe.db.con.execute("SELECT count(*) FROM daily_quotes").fetchone()[0]
    r2 = pipe.ingest_date(D)
    n2 = pipe.db.con.execute("SELECT count(*) FROM daily_quotes").fetchone()[0]
    assert r2.ok and n1 == n2 == r1.rows["TWSE"] + r1.rows["TPEX"]
    # 第二次使用原始檔快取，不再發出請求
    assert pipe.fetcher.calls.count(("twse_quotes", "20261113")) == 1
    pipe.db.refresh_securities()
    row = pipe.db.con.execute("SELECT market, sec_type FROM securities WHERE code = '2330'").fetchone()
    assert row == ("TWSE", "stock")


def test_not_published_marks_failed_and_drops_raw(pipe):
    other = dt.date(2026, 11, 12)
    r = pipe.ingest_date(other)
    assert not r.ok and r.status == {"TWSE": "failed", "TPEX": "failed"}
    assert not pipe.raw.exists("twse_quotes", "20261112")


def test_backfill_skips_ok_days(pipe):
    def quote_calls():
        return [c for c in pipe.fetcher.calls if c[0] != "twse_taiex"]

    pipe.backfill(D, D, today=dt.date(2026, 11, 14))
    calls = len(quote_calls())
    results = pipe.backfill(D, D, today=dt.date(2026, 11, 14))
    # 已成功的日子不再抓行情（當月日曆仍會重抓，屬預期）
    assert results == [] and len(quote_calls()) == calls


def _q(code, market, o, h, low, c, sec_type="stock"):
    return Quote(D, market, code, code, sec_type, o, h, low, c, 1000, 10000, 1)


def test_quality_flags_ohlc_and_duplicates():
    db = Database()
    db.replace_quotes(D, "TWSE", [_q("1101", "TWSE", 10, 11, 9, 10), _q("1102", "TWSE", 10, 9, 8, 10)])
    db.replace_quotes(D, "TPEX", [_q("1101", "TPEX", 10, 11, 9, 10)])
    results = quality.check_date(db, D)
    checks = {(m, c): s for m, c, s, _ in results}
    assert checks[("TWSE", "ohlc_consistency")] == "error"  # 1/2 > 1%
    assert checks[("ALL", "cross_market_duplicate")] == "error"
    assert quality.has_error(results, "TPEX")  # 跨市場重複影響兩邊
    db.close()


def test_replace_quotes_writes_null_not_nan():
    db = Database()
    no_trade = Quote(D, "TWSE", "1101", "x", "stock", None, None, None, None, 0, 0, 0)
    db.replace_quotes(D, "TWSE", [no_trade, _q("1102", "TWSE", 10, 11, 9, 10)])
    rows = db.con.execute("SELECT code, close IS NULL, isnan(coalesce(close, 0)), volume "
                          "FROM daily_quotes ORDER BY code").fetchall()
    assert rows == [("1101", True, False, 0), ("1102", False, False, 1000)]
    db.close()


def test_quality_row_count_drop():
    db = Database()
    for i in range(6):
        day = D - dt.timedelta(days=10 - i)
        db.set_status(day, "TWSE", "ok", 100)
    db.replace_quotes(D, "TWSE", [_q(f"{1100 + i}", "TWSE", 10, 11, 9, 10) for i in range(50)])
    results = quality.check_date(db, D)
    assert ("TWSE", "row_count", "error") in {(m, c, s) for m, c, s, _ in results}
    db.close()


def test_quality_failure_marks_status_failed(tmp_path):
    db = Database()
    bad = (FIX / "twse_quotes_20261113.json").read_text(encoding="utf-8")
    for i in range(6):
        db.set_status(D - dt.timedelta(days=10 - i), "TWSE", "ok", 100_000)
    p = Pipeline(db, RawStore(tmp_path / "raw"), FakeFetcher({("twse_quotes", "20261113"): bad}))
    r = p.ingest_date(D)
    assert r.status["TWSE"] == "failed" and db.status(D, "TWSE") == "failed"
    db.close()
