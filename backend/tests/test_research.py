import datetime as dt

import numpy as np
import pandas as pd
import pytest

from apex.backtest.data import MarketData
from apex.research.features import Features, financial_frame, revenue_frame, to_days
from apex.research.regime import by_regime, classify

PRICE_FEATURES = ["ret_20", "mom_120_20", "ma_gap_60", "ma_slope_60", "high_gap_120", "vol_20", "downvol_20",
                  "mdd_60", "gap_5", "value_20", "value_ratio_5_20", "rank_ret_20"]


def _random_data(n_days: int = 300, n_codes: int = 6, seed: int = 0) -> MarketData:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2020-01-01", periods=n_days)
    codes = [f"{1101 + i}" for i in range(n_codes)]
    px = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.02, (n_days, n_codes)), axis=0)),
                      index=days, columns=codes)
    px.iloc[100:110, 0] = np.nan  # 停止買賣
    px.iloc[:50, 1] = np.nan  # 晚上市
    opn = px * (1 + rng.normal(0, 0.005, px.shape))
    panels = {"open": opn, "close": px, "adj_open": opn, "adj_close": px, "volume": px * 0 + 1e6,
              "value": px * 1e6}
    sec = pd.DataFrame({c: {"sec_type": "stock", "status": "listed", "last_date": None, "delisted_date": None,
                            "industry": "半導體業", "name": c} for c in codes}, dtype=object).T
    return MarketData(days, panels, sec, None)


def _truncate(data: MarketData, i: int) -> MarketData:
    days = data.days[:i + 1]
    return MarketData(days, {k: v.iloc[:i + 1] for k, v in data.panels.items()}, data.securities, None)


@pytest.mark.parametrize("name", PRICE_FEATURES)
def test_price_features_have_no_lookahead(name):
    """在 t 當天截斷資料重算，t 的特徵值必須與用全期資料算的一樣。"""
    data = _random_data()
    full = Features(data)[name]
    for i in (130, 200, 299):
        part = Features(_truncate(data, i))[name]
        pd.testing.assert_series_equal(full.iloc[i], part.iloc[i], check_names=False)


def test_price_features_values():
    data = _random_data()
    f = Features(data)
    px = data["adj_close"]
    assert f["ret_20"].iloc[60, 2] == pytest.approx(px.iloc[60, 2] / px.iloc[40, 2] - 1, rel=1e-6)
    assert f["mom_120_20"].iloc[200, 3] == pytest.approx(px.iloc[180, 3] / px.iloc[80, 3] - 1, rel=1e-6)
    assert np.isnan(f["ret_20"].iloc[60, 1])  # 上市未滿 20 天
    # 停止買賣期間沿用最後價格，報酬不中斷
    assert f["ret_20"].iloc[105, 0] == pytest.approx(px.iloc[99, 0] / px.iloc[85, 0] - 1, rel=1e-5)
    assert ((f["rank_ret_20"].dropna(how="all") <= 1) | f["rank_ret_20"].dropna(how="all").isna()).all().all()


def test_to_days_starts_on_next_trading_day_and_expires():
    days = pd.bdate_range("2024-03-01", periods=30)
    df = pd.DataFrame({"code": ["A", "A"], "available_date": [dt.date(2024, 3, 9), dt.date(2024, 3, 20)],
                       "x": [1.0, 2.0]})
    p = to_days(df, days, stale=7)["x"]["A"]
    assert np.isnan(p[pd.Timestamp("2024-03-08")])  # 公告前看不到
    assert p[pd.Timestamp("2024-03-11")] == 1.0  # 3/9 週六 → 下一個交易日生效
    assert p[pd.Timestamp("2024-03-19")] == 1.0
    assert p[pd.Timestamp("2024-03-20")] == 2.0
    assert p[pd.Timestamp("2024-03-29")] == 2.0  # 之後第 7 個交易日仍沿用
    assert np.isnan(p[pd.Timestamp("2024-04-01")])  # 超過 stale 天數


def test_revenue_frame_growth_and_availability():
    rows = [("A", 2023, m, 100.0) for m in range(1, 13)]
    rows += [("A", 2024, m, 100.0 + 10 * m) for m in range(1, 7)]
    rev = pd.DataFrame(rows, columns=["code", "year", "month", "revenue"])
    out = revenue_frame(rev).set_index("available_date")
    may = out.loc[dt.date(2024, 6, 11)]  # 2024-05 營收在 6/11 起可用
    assert may["rev_yoy"] == pytest.approx(0.5)
    assert may["rev_yoy_3m"] == pytest.approx((130 + 140 + 150) / 300 - 1)
    assert np.isnan(may["rev_yoy_12m"])  # 2022 年資料不足
    assert may["rev_up_streak"] == 5
    assert dt.date(2024, 1, 11) not in out.index  # 2023-12 沒有去年同期 → 不輸出


def test_revenue_frame_requires_consecutive_months():
    rows = [("A", 2023, m, 100.0) for m in range(1, 13) if m != 3] + [("A", 2024, 4, 120.0)]
    out = revenue_frame(pd.DataFrame(rows, columns=["code", "year", "month", "revenue"]))
    apr = out[out["available_date"] == dt.date(2024, 5, 11)].iloc[0]
    assert apr["rev_yoy"] == pytest.approx(0.2)
    assert np.isnan(apr["rev_yoy_3m"])  # 2023-03 缺漏


def test_financial_frame():
    def q(year, quarter, eps, rev, gp, net, equity):
        return {"code": "A", "year": year, "quarter": quarter,
                "available_date": dt.date(year, 3 * quarter, 28),
                "eps": eps, "revenue": rev, "gross_profit": gp, "operating_income": gp / 2,
                "net_income": net, "net_income_parent": net, "equity_parent": equity, "equity": equity,
                "total_liabilities": 50.0, "total_assets": 200.0, "share_capital": 1000.0}
    fin = pd.DataFrame([q(2023, 1, 1.0, 100, 40, 10, 100), q(2023, 2, 1.0, 100, 40, 10, 100),
                        q(2023, 3, 1.0, 100, 40, 10, 100), q(2023, 4, 1.0, 100, 40, 10, 100),
                        q(2024, 1, 1.5, 100, 50, 15, 120)])
    out = financial_frame(fin).set_index("available_date")
    last = out.loc[dt.date(2024, 3, 28)]
    assert last["eps_yoy"] == pytest.approx(0.5)
    assert last["gm_chg"] == pytest.approx(0.10)
    assert last["roe_ttm"] == pytest.approx(45 / 120)
    assert last["debt_ratio"] == pytest.approx(0.25)
    assert last["shares"] == 100
    assert np.isnan(out.loc[dt.date(2023, 3, 28), "eps_yoy"])  # 沒有去年同季
    assert last["eps_ttm"] == pytest.approx(4.5)  # 1＋1＋1＋1.5
    assert np.isnan(out.loc[dt.date(2023, 3, 28), "eps_ttm"])  # 不足 4 季
    assert last["bvps"] == pytest.approx(120 / 100)  # 沒有每股淨值時用權益 ÷ 估計股數


def test_payout_panel_counts_one_year_from_ex_date():
    from apex.research.features import YEAR_DAYS, payout_panel
    days = pd.bdate_range("2023-01-02", periods=YEAR_DAYS + 20)
    actions = pd.DataFrame({"code": ["A", "A"], "eff_date": [days[5], days[10]],
                            "prev_close": [100.0, 50.0], "ref_price": [95.0, 49.0]})
    out = payout_panel(actions, days, pd.Index(["A", "B"]))
    assert out.loc[days[4], "A"] == 0  # 除息日前還沒計入
    assert out.loc[days[5], "A"] == pytest.approx(0.05)
    assert out.loc[days[10], "A"] == pytest.approx(0.07)
    assert out.loc[days[5 + YEAR_DAYS], "A"] == pytest.approx(0.02)  # 一年後第一筆移出
    assert (out["B"] == 0).all()


def test_classify_regimes():
    days = pd.bdate_range("2018-01-01", periods=1200)
    up = pd.Series(np.linspace(100, 300, 1200) * (1 + 0.001 * np.sin(np.arange(1200))), index=days)
    reg = classify(up)
    assert (reg["regime"].iloc[:244] == "資料不足").all()
    assert (reg["regime"].iloc[-200:].isin(["多頭", "高波動"])).all()
    assert (reg["regime"].iloc[-200:] == "多頭").mean() > 0.5
    down = pd.Series(np.linspace(300, 100, 1200), index=days)
    assert (classify(down)["regime"].iloc[-100:].isin(["空頭", "高波動"])).all()


def test_regime_has_no_lookahead():
    rng = np.random.default_rng(1)
    days = pd.bdate_range("2018-01-01", periods=900)
    s = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 900))), index=days)
    full = classify(s)
    for i in (400, 700):
        part = classify(s.iloc[:i + 1])
        assert part.iloc[i]["regime"] == full.iloc[i]["regime"]
        assert part.iloc[i]["vol_pct"] == pytest.approx(full.iloc[i]["vol_pct"])


def test_by_regime_uses_previous_day_state():
    days = pd.bdate_range("2024-01-01", periods=4)
    ret = pd.Series([0.0, 0.01, -0.02, 0.03], index=days)
    regime = pd.Series(["多頭", "空頭", "多頭", "多頭"], index=days)
    out = by_regime(ret, regime)
    assert out.loc["多頭", "days"] == 2  # 第 2、4 天（前一天為多頭）
    assert out.loc["空頭", "days"] == 1
