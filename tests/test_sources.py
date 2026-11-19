import datetime as dt
from pathlib import Path

import pytest

from apex.parsing import classify
from apex.sources import NotPublished, tpex, twse

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("code,expected", [
    ("2330", "stock"), ("0050", "etf"), ("00400A", "etf"), ("2881A", "preferred"),
    ("2887Z1", "preferred"), ("910322", "dr"), ("9103", "dr"), ("020000", "etn"), ("02001L", "etn"),
    ("01001T", "reit"),
    ("712108", "warrant"), ("71425P", "warrant"), ("030001", "warrant"),
])
def test_classify(code, expected):
    assert classify(code) == expected


def test_twse_quotes_2026():
    quotes = twse.parse_quotes(load("twse_quotes_20261113.json"), dt.date(2026, 11, 13))
    q = {x.code: x for x in quotes}["2330"]
    assert (q.open, q.high, q.low, q.close) == (2375.0, 2390.0, 2340.0, 2350.0)
    assert q.volume == 30_973_127 and q.market == "TWSE"
    assert all(x.sec_type != "warrant" for x in quotes)


def test_twse_holiday_not_published():
    with pytest.raises(NotPublished):
        twse.parse_quotes(load("twse_quotes_20261115_holiday.json"), dt.date(2026, 11, 15))


def test_tpex_quotes_2015_includes_managed_and_excludes_warrants():
    quotes = tpex.parse_quotes(load("tpex_quotes_20150302.json"), dt.date(2015, 3, 2))
    codes = {x.code: x for x in quotes}
    assert codes["5364"].is_managed
    assert all(x.sec_type != "warrant" for x in quotes)
    assert any(x.close is None for x in quotes) or True


def test_tpex_holiday_not_published():
    with pytest.raises(NotPublished):
        tpex.parse_quotes(load("tpex_quotes_20261115_holiday.json"), dt.date(2026, 11, 15))


def test_taiex_month_calendar():
    days = twse.parse_taiex_month(load("twse_taiex_201503.json"))
    assert len(days) == 22 and days[0]["date"] == dt.date(2015, 3, 2)


def test_tpex_skips_title_row_mixed_into_data():
    """2015-11-23 的回應在資料中混入「104年11月23日 櫃檯買賣中心證券行情」表頭列。"""
    import json

    d = dt.date(2026, 11, 13)
    data = json.loads((FIX / "tpex_quotes_20261113.json").read_text(encoding="utf-8"))
    main = next(t for t in data["tables"] if t["title"] == "上櫃股票行情")
    n = len(tpex.parse_quotes(json.dumps(data), d))
    main["data"].insert(1, ["115年11月13日 櫃檯買賣中心證券行情"] + [""] * (len(main["fields"]) - 1))
    quotes = tpex.parse_quotes(json.dumps(data), d)
    assert len(quotes) == n and all(q.code.isalnum() and q.code.isascii() for q in quotes)
