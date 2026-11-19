import datetime as dt
from pathlib import Path

import pytest

from apex.db import Database
from apex.sources import NotPublished, chips, fundamentals, twse

FIX = Path(__file__).parent / "fixtures"
D26, D15 = dt.date(2026, 11, 13), dt.date(2015, 3, 2)


def _read(name):
    return (FIX / name).read_text(encoding="utf-8")


def _by_code(rows):
    return {r.code: r for r in rows}


# --- 三大法人 ---------------------------------------------------------------

def test_twse_insti_new_format_merges_foreign_dealer():
    r = _by_code(chips.parse_twse_insti(_read("twse_insti_20261113.json"), D26))["2330"]
    assert (r.foreign_net, r.foreign_dealer_net, r.trust_net, r.dealer_net, r.total_net) == (
        -2618070, 0, 2946069, 415614, 743613)
    assert r.dealer_self_net + r.dealer_hedge_net == r.dealer_net


def test_twse_insti_old_format():
    r = _by_code(chips.parse_twse_insti(_read("twse_insti_20150302.json"), D15))["2330"]
    assert r.foreign_dealer_net is None
    assert r.foreign_net + r.trust_net + r.dealer_net == r.total_net == -5606574


@pytest.mark.parametrize("fixture,date", [("tpex_insti_20261113.json", D26),
                                          ("tpex_insti_20150302.json", D15)])
def test_tpex_insti_totals_consistent(fixture, date):
    rows = chips.parse_tpex_insti(_read(fixture), date)
    assert rows and all(r.market == "TPEX" for r in rows)
    for r in rows:
        assert r.foreign_net + r.trust_net + r.dealer_net == r.total_net


def test_insti_rejects_wrong_date():
    with pytest.raises(Exception, match="不符"):
        chips.parse_twse_insti(_read("twse_insti_20261113.json"), D15)


def test_margin_both_markets():
    tw = _by_code(chips.parse_twse_margin(_read("twse_margin_20261113.json"), D26))["2330"]
    assert (tw.margin_buy, tw.margin_sell, tw.margin_balance, tw.short_sell, tw.short_buy, tw.short_balance,
            tw.offset) == (680, 477, 32376, 5, 20, 50, 3)
    tp = _by_code(chips.parse_tpex_margin(_read("tpex_margin_20261113.json"), D26))["5483"]
    assert (tp.margin_buy, tp.margin_balance, tp.short_sell, tp.short_balance) == (218, 12968, 6, 392)


def test_replace_daily_roundtrip():
    db = Database()
    rows = chips.parse_twse_insti(_read("twse_insti_20261113.json"), D26)
    db.replace_daily("institutional", D26, "TWSE", rows)
    db.replace_daily("institutional", D26, "TWSE", rows)  # 重複執行結果相同
    got = db.con.execute("SELECT count(*), sum(total_net) FROM institutional").fetchone()
    assert got == (len(rows), sum(r.total_net for r in rows))
    db.close()


# --- 月營收、財報 -------------------------------------------------------------

def test_revenue_units_industry_and_availability():
    rows = fundamentals.parse_revenue(_read("mops_revenue_sii_202610.html"), "TWSE", 2026, 10)
    r = _by_code(rows)["1101"]
    assert r.industry == "水泥工業" and r.revenue == 16_357_176_000  # 千元 → 元
    assert r.available_date == dt.date(2026, 11, 11)
    assert {x.industry for x in rows} == {"水泥工業", "食品工業"}  # 合計列不收錄
    assert fundamentals.revenue_available(2026, 12) == dt.date(2027, 1, 11)


def test_revenue_rejects_wrong_month():
    with pytest.raises(Exception, match="不符"):
        fundamentals.parse_revenue(_read("mops_revenue_sii_202610.html"), "TWSE", 2026, 9)


def test_financials_layouts_and_units():
    inc = _by_code(fundamentals.parse_financials(_read("mops_income_sii_2026Q2.html"), "income"))
    assert inc["2330"].layout == "general" and inc["2330"].values["eps"] == 49.33
    assert inc["2330"].values["revenue"] == 2_404_483_690_000
    assert inc["2881"].layout == "financial" and inc["2881"].values["revenue"] == 172_468_262_000  # 淨收益
    bal = _by_code(fundamentals.parse_financials(_read("mops_balance_sii_2026Q2.html"), "balance"))
    assert bal["2330"].values["bvps"] == 248.05 and bal["2330"].values["total_assets"] == 9_375_654_727_000


def test_financials_not_published():
    with pytest.raises(NotPublished):
        fundamentals.parse_financials("<html><body>查無所需資料！</body></html>", "income")


def test_financial_deadlines():
    assert fundamentals.fin_available(2026, 1, False) == dt.date(2026, 5, 16)
    assert fundamentals.fin_available(2026, 2, True) == dt.date(2026, 9, 1)
    assert fundamentals.fin_available(2026, 4, False) == dt.date(2027, 4, 1)


def test_financials_view_quarterly_and_available_date():
    db = Database()
    for q, ytd in ((1, 100.0), (2, 250.0), (3, 450.0)):
        row = fundamentals.FinRow("income", "1111", "x", "general",
                                  {k: ytd if k == "revenue" else None for k in fundamentals.INCOME_ITEMS})
        db.replace_financials("income", "s", "TWSE", 2026, q, [row], True)
    got = db.con.execute(
        "SELECT quarter, revenue, available_date FROM financials_q ORDER BY quarter").fetchall()
    assert got == [(1, 100.0, dt.date(2026, 5, 16)), (2, 150.0, dt.date(2026, 8, 15)),
                   (3, 200.0, dt.date(2026, 11, 15))]
    db.con.execute("DELETE FROM fin_income WHERE quarter = 2")  # 缺 Q2 時 Q3 單季無法計算
    assert db.con.execute("SELECT revenue FROM financials_q WHERE quarter = 3").fetchone()[0] is None
    db.close()


# --- 開休市日期、指數 ---------------------------------------------------------

def test_holidays_and_expected_trading_days():
    rows = twse.parse_holidays(_read("twse_holidays_2027.json"), 2027)
    closed = {d for d, _, c in rows if c}
    assert dt.date(2027, 2, 2) in closed  # 僅辦理結算交割
    assert dt.date(2027, 2, 1) not in closed  # 春節前最後交易日
    with pytest.raises(NotPublished):
        twse.parse_holidays(_read("twse_holidays_2027.json"), 2028)
    db = Database()
    db.con.execute("INSERT INTO trading_days VALUES ('2027-01-29', 'test', now())")
    db.replace_holidays(2027, rows)
    days = db.expected_trading_days(dt.date(2027, 1, 29), dt.date(2027, 2, 15))
    assert days == [dt.date(2027, 1, 29), dt.date(2027, 2, 1), dt.date(2027, 2, 11), dt.date(2027, 2, 12),
                    dt.date(2027, 2, 15)]
    db.close()


def test_taiex_total_return_index():
    rows = twse.parse_taiex_tr_month(_read("twse_taiex_tr_202611.json"))
    assert rows[0] == (dt.date(2026, 11, 2), 103873.24)


# --- FinMind 補齊已下市櫃公司 ---------------------------------------------------

def test_finmind_revenue_uses_revenue_month():
    from apex.sources import finmind
    rows = finmind.parse_revenue(_read("finmind_revenue_2809.json"), "2809", "TWSE", "京城銀")
    assert (rows[0].year, rows[0].month, rows[0].revenue) == (2015, 1, 482901000)  # 2014-12 的資料被略過
    assert rows[0].available_date == dt.date(2015, 2, 11)


def test_finmind_income_single_quarter_to_ytd():
    from apex.sources import finmind
    inc = finmind.parse_income(_read("finmind_income_2330.json"), "2330", "台積電")
    assert inc[(2025, 2)].values["revenue"] == pytest.approx(1_773_045_533_000)  # 與觀測站年初累計一致
    assert inc[(2025, 2)].values["eps"] == pytest.approx(29.31)
    bank = finmind.parse_income(_read("finmind_income_2809.json"), "2809", "京城銀")[(2020, 1)]
    assert bank.layout == "financial" and bank.values["revenue"] > 0  # 銀行以淨收益為營收


def test_finmind_balance():
    from apex.sources import finmind
    bal = finmind.parse_balance(_read("finmind_balance_2330.json"), "2330", "台積電")[(2025, 2)]
    assert bal.values["total_assets"] == 7006349549000 and bal.layout == "general"


def test_finmind_quota_is_not_published():
    from apex.sources import finmind
    with pytest.raises(NotPublished):
        finmind.parse_revenue('{"msg": "Requests reach the upper limit.", "status": 402}', "1", "TWSE", "x")


def test_insert_supplement_keeps_mops_rows():
    db = Database()
    mops = fundamentals.Revenue(2026, 1, "TWSE", "1111", "x", "水泥工業", 100, None, None, None, None,
                                dt.date(2026, 2, 11))
    db.replace_revenue("mops", "202601", 2026, 1, [mops], True)
    import pandas as pd
    df = pd.DataFrame([vars(mops) | {"revenue": 999}, vars(mops) | {"month": 2, "revenue": 200,
                                                                    "available_date": dt.date(2026, 3, 11)}])
    df["source"], df["fetched_at"] = "finmind", dt.datetime(2026, 1, 1)
    assert db.insert_supplement("monthly_revenue", "finmind", "1111", df) == 1
    got = db.con.execute("SELECT month, revenue, source FROM monthly_revenue ORDER BY month").fetchall()
    assert got == [(1, 100, "mops"), (2, 200, "finmind")]
    db.close()
