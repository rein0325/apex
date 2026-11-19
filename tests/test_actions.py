import datetime as dt
from pathlib import Path

import pytest

from apex import quality
from apex.db import Database
from apex.sources import Quote
from apex.sources.actions import SPEC_BY_SOURCE, Action, parse

FIX = Path(__file__).parent / "fixtures"


def _parse(source, fixture):
    return parse(SPEC_BY_SOURCE[source], (FIX / fixture).read_text(encoding="utf-8"))


def test_twse_exright_uses_dividend_only_reference():
    rows = {(a.code, a.date): a for a in _parse("twse_exright", "twse_exright_2026.json")}
    assert len(rows) == 5
    # 現增除權：還原用減除股利參考價（44.40），理論參考價 30.04 另存
    rights = rows[("6225", dt.date(2026, 8, 18))]
    assert (rights.prev_close, rights.ref_price, rights.theo_ref) == (44.40, 44.40, 30.04)
    assert rights.factor == 1.0
    # 純除息：參考價相同，不另存理論價
    cash = rows[("2330", dt.date(2026, 3, 17))]
    assert cash.ref_price == 1838.99 and cash.theo_ref is None and cash.kind == "dividend"
    assert cash.factor == pytest.approx(1838.99 / 1845)


def test_tpex_reduction_roc_compact_date():
    rows = _parse("tpex_reduction", "tpex_reduction_2015.json")
    a = next(r for r in rows if r.code == "4131")
    assert (a.date, a.market, a.kind, a.prev_close, a.ref_price) == (
        dt.date(2015, 1, 30), "TPEX", "reduction", 9.95, 12.56)
    assert a.theo_ref is None  # 「除權參考價」0.00 視為無


def test_etf_split_0050():
    a = next(r for r in _parse("twse_etfsplit", "twse_etfsplit_2025.json") if r.code == "0050")
    assert a.kind == "split" and a.date == dt.date(2025, 6, 18)
    assert a.factor == pytest.approx(0.25, abs=0.001)


def test_empty_result_returns_empty_list():
    assert _parse("twse_parvalue", "twse_parvalue_2015_empty.json") == []


# --- 還原因子 --------------------------------------------------------------

DAYS = [dt.date(2026, 7, d) for d in (6, 7, 8, 9, 13, 14)]  # 7/10 颱風休市


def _db_with_quotes(closes: dict[str, list[float | None]]) -> Database:
    db = Database()
    for i, d in enumerate(DAYS):
        db.con.execute("INSERT INTO trading_days VALUES (?, 'test', now())", [d])
        quotes = [Quote(d, "TWSE", code, code, "stock", cs[i], cs[i], cs[i], cs[i], 1000, 1000, 1)
                  for code, cs in closes.items() if cs[i] is not None]
        db.replace_quotes(d, "TWSE", quotes)
        db.set_status(d, "TWSE", "ok", len(quotes))
    return db


def _act(code, date, prev, ref, theo=None, kind="dividend"):
    return Action(date, "TWSE", code, code, kind, prev, ref, theo)


def test_adjustment_maps_closed_day_and_chains_events():
    db = _db_with_quotes({"1111": [100, 100, 100, 100, 90, 45]})
    # 除息公告 7/10（休市）→ 實際 7/13 生效；7/14 一拆二
    db.replace_actions("t1", "TWSE", "dividend", 2026, [_act("1111", dt.date(2026, 7, 10), 100, 90)], True)
    split = _act("1111", dt.date(2026, 7, 14), 90, 45, kind="split")
    db.replace_actions("t2", "TWSE", "split", 2026, [split], True)
    assert db.refresh_adjustments() == 2
    adj = dict(db.con.execute("SELECT date, adj_close FROM daily_quotes_adj WHERE code = '1111'").fetchall())
    assert adj[dt.date(2026, 7, 14)] == 45  # 最新價格不變
    assert adj[dt.date(2026, 7, 13)] == pytest.approx(45)
    assert adj[dt.date(2026, 7, 9)] == pytest.approx(45)  # 100 × 0.9 × 0.5
    assert adj[dt.date(2026, 7, 6)] == pytest.approx(45)
    db.close()


def test_future_event_does_not_adjust_current_prices():
    db = _db_with_quotes({"1111": [100] * 6})
    db.replace_actions("t1", "TWSE", "dividend", 2026, [_act("1111", dt.date(2026, 7, 20), 100, 95)], False)
    assert db.refresh_adjustments() == 0
    db.close()


def test_replace_actions_is_per_source():
    db = Database()
    db.replace_actions("a", "TPEX", "split", 2026, [_act("00001", dt.date(2026, 1, 5), 10, 5)], True)
    db.replace_actions("b", "TPEX", "split", 2026, [_act("00002", dt.date(2026, 2, 5), 10, 20)], True)
    assert db.con.execute("SELECT count(*) FROM corporate_actions").fetchone()[0] == 2
    db.close()


# --- 品質檢查 ---------------------------------------------------------------

def _extreme(db, date):
    return [r for r in quality.check_date(db, date) if r[1] == "extreme_move"]


def test_extreme_move_uses_reference_price_and_rights_band(monkeypatch):
    monkeypatch.setattr(quality, "NEW_LISTING_DAYS", 3)  # 測試資料只有 4 個前期交易日
    # 1111：除息後跌 10%（相對前收）但相對參考價正常
    # 2222：現增除權，價格落在 [理論參考價, 減除股利參考價] 之間
    # 3333：無事件，真的跌 20%
    db = _db_with_quotes({"1111": [100] * 4 + [81, 81], "2222": [50] * 4 + [42, 42],
                          "3333": [10] * 4 + [8, 8]})
    db.replace_actions("t", "TWSE", "dividend", 2026, [
        _act("1111", dt.date(2026, 7, 10), 100, 90),
        _act("2222", dt.date(2026, 7, 13), 50, 50, theo=40),
    ], True)
    results = _extreme(db, dt.date(2026, 7, 13))
    assert len(results) == 1 and "3333" in results[0][3] and "1111" not in results[0][3]
    db.close()


def test_extreme_move_skips_new_listing():
    db = _db_with_quotes({"4444": [None, None, None, 10, 20, 20]})  # 7/9 才上市
    assert _extreme(db, dt.date(2026, 7, 13)) == []
    db.close()
