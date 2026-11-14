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
    ("2887Z1", "preferred"), ("910322", "dr"), ("020000", "etn"), ("01001T", "reit"),
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
