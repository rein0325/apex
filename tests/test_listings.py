import datetime as dt
from pathlib import Path

import pytest

from apex.db import Database
from apex.parsing import classify
from apex.sources import ParseError, Quote
from apex.sources.listings import (
    SECTION_TYPES,
    Delisting,
    parse_isin,
    parse_tpex_delisted,
    parse_twse_delisted,
)

FIX = Path(__file__).parent / "fixtures"


def _read(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_parse_isin_sections_industry_and_skips_warrants():
    rows = {r.code: r for r in parse_isin(_read("isin_twse_202611.html"), "TWSE")}
    assert not any(classify(c) == "warrant" for c in rows)  # 030001 等權證不收錄
    tp = rows["1101"]
    assert (tp.name, tp.industry, tp.listed_date, tp.section) == (
        "台泥", "水泥工業", dt.date(1962, 2, 9), "股票")
    assert rows["2237"].board == "上市臺灣創新板" and rows["2237"].section == "創新板"
    assert rows["00400A"].industry is None
    # 一覽表分段與代號規則一致（9103 是 4 碼存託憑證）
    assert all(SECTION_TYPES[r.section] == classify(r.code) for r in rows.values())
    assert classify("9103") == "dr"


def test_parse_isin_rejects_unexpected_page():
    with pytest.raises(ParseError):
        parse_isin("<html>系統維護中</html>", "TWSE")


def test_parse_twse_delisted():
    rows = parse_twse_delisted(_read("twse_delisted_20261118.json"))
    assert rows[0] == Delisting(dt.date(2026, 11, 18), "TWSE", "1589", "永冠-KY")
    assert rows[-1].date == dt.date(2001, 1, 20)


def test_parse_tpex_delisted_keeps_reason():
    rows = parse_tpex_delisted(_read("tpex_delisted_2019.json"))
    assert (rows[0].code, rows[0].date, rows[0].market) == ("1795", dt.date(2019, 12, 16), "TPEX")
    assert rows[0].reason.startswith("本中心")


# --- 證券主檔狀態 -----------------------------------------------------------

def _quote(d, market, code):
    return Quote(d, market, code, code, "stock", 10, 10, 10, 10, 1, 1, 1)


def test_refresh_securities_status():
    db = Database()
    d1, d2 = dt.date(2026, 1, 5), dt.date(2026, 1, 6)
    for d, quotes in [(d1, {"TWSE": ["1111", "2222", "3333"], "TPEX": ["4444"]}),
                      (d2, {"TWSE": ["1111", "4444", "5555"], "TPEX": ["6666"]})]:
        for market, codes in quotes.items():
            db.replace_quotes(d, market, [_quote(d, market, c) for c in codes])
            db.set_status(d, market, "ok", len(codes))
    listing = parse_isin(_read("isin_twse_202611.html"), "TWSE")[0]
    db.replace_listing_snapshot("2026-01", [
        listing.__class__(**{**vars(listing), "code": c}) for c in ("1111", "4444", "5555", "6666")])
    db.replace_delistings("t", [
        Delisting(dt.date(2026, 1, 20), "TWSE", "2222", "x"),
        Delisting(dt.date(2010, 1, 4), "TWSE", "3333", "舊代號"),  # 首次交易前的紀錄不算
        Delisting(dt.date(2026, 1, 6), "TPEX", "4444", "轉上市"),
        Delisting(dt.date(2026, 1, 7), "TWSE", "5555", "當天仍在一覽表"),
        Delisting(dt.date(2026, 1, 7), "TPEX", "6666", "轉上市，尚未在上市交易"),
    ])
    db.refresh_securities()
    got = {r[0]: r[1:] for r in db.con.execute(
        "SELECT code, status, delisted_date, industry, market FROM securities").fetchall()}
    assert got["1111"] == ("listed", None, "水泥工業", "TWSE")
    assert got["2222"] == ("delisted", dt.date(2026, 1, 20), None, "TWSE")
    assert got["3333"][:2] == ("ended", None)
    assert got["4444"][:2] == ("listed", None) and got["4444"][3] == "TWSE"  # 上櫃轉上市
    assert got["5555"][:2] == ("delisted", dt.date(2026, 1, 7))
    assert got["6666"][0] == "listed"  # 一覽表已列在上市
    db.close()


def test_replace_delistings_by_year_keeps_other_years():
    db = Database()
    db.replace_delistings("tp", [Delisting(dt.date(2019, 5, 1), "TPEX", "1234", "a")], 2019, True)
    db.replace_delistings("tp", [Delisting(dt.date(2020, 5, 1), "TPEX", "5678", "b")], 2020, False)
    db.replace_delistings("tp", [], 2020, True)
    assert db.con.execute("SELECT code FROM delistings").fetchall() == [("1234",)]
    assert db.complete_action_years("tp") == {2019, 2020}
    db.close()
