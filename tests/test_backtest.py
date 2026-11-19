import datetime as dt

import pandas as pd
import pytest

from apex.backtest import CostModel, run, summarize
from apex.backtest.data import MarketData, PointInTime
from apex.backtest.engine import rebalance_dates
from apex.backtest.metrics import yearly_returns
from apex.db import Database
from apex.sources.fundamentals import Revenue

DAYS = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"])
NO_COST = CostModel(commission_rate=0, min_commission=0, tax_stock=0, tax_etf=0, slippage_bps=0,
                    max_participation=1.0)


def _data(prices: dict[str, list[float | None]], sec: dict[str, dict] | None = None,
          adj: dict[str, list[float]] | None = None) -> MarketData:
    """開盤＝收盤＝prices；adj 提供還原價（預設與原始價相同）。"""
    raw = pd.DataFrame(prices, index=DAYS, dtype=float)
    adj_df = pd.DataFrame(adj, index=DAYS, dtype=float) if adj else raw
    panels = {"open": raw, "close": raw, "adj_open": adj_df, "adj_close": adj_df,
              "volume": raw * 0 + 1e9, "value": raw * 0 + 1e12}
    securities = pd.DataFrame({c: {"sec_type": "stock", "status": "listed", "last_date": None,
                                   "delisted_date": None, "industry": None, "name": c} for c in prices},
                              dtype=object).T
    for code, info in (sec or {}).items():
        for k, v in info.items():
            securities.loc[code, k] = v
    return MarketData(DAYS, panels, securities, None)


def _hold(code):
    return lambda ctx: {code: 1.0}


def test_signal_at_close_fills_next_open():
    data = _data({"A": [10, 10.5, 10.5, 10.5, 10.5]})
    r = run(data, _hold("A"), dt.date(2026, 1, 5), dt.date(2026, 1, 9), 1000, "once", NO_COST)
    assert r.trades.iloc[0]["date"] == dt.date(2026, 1, 6)  # 1/5 收盤訊號，1/6 開盤成交
    assert r.trades.iloc[0]["price"] == 10.5
    assert r.equity.iloc[-1] == pytest.approx(1000)  # 以 10.5 買進，沒賺到 1/5→1/6 的漲幅


def test_dividend_reinvested_through_adjusted_prices():
    # 1/7 除息 2 元：原始價 20→18，還原價不跳動
    data = _data({"A": [20, 20, 18, 18, 18]}, adj={"A": [18, 18, 18, 18, 18]})
    r = run(data, _hold("A"), dt.date(2026, 1, 5), dt.date(2026, 1, 9), 1000, "once", NO_COST)
    assert r.equity.iloc[-1] == pytest.approx(1000)  # 股利再投入，資產不因除息下降


def test_limit_up_open_blocks_buy():
    data = _data({"A": [10, 11, 11, 11, 11]})  # 1/6 開盤漲停
    r = run(data, _hold("A"), dt.date(2026, 1, 5), dt.date(2026, 1, 9), 1000, "D", NO_COST)
    assert r.trades.iloc[0]["date"] == dt.date(2026, 1, 7)
    assert any("漲停" in e for e in r.events)


def test_costs_commission_tax_slippage():
    costs = CostModel(commission_discount=0.5, min_commission=20, slippage_bps=10)
    assert costs.commission(100_000) == pytest.approx(71.25)
    assert costs.commission(1_000) == 20  # 最低手續費
    assert costs.tax(100_000, "stock", "2330") == 300
    assert costs.tax(100_000, "etf", "0050") == 100
    assert costs.tax(100_000, "etf", "00679B") == 0
    assert costs.buy_price(100) == pytest.approx(100.1)


def test_round_trip_costs_reduce_equity():
    data = _data({"A": [100] * 5})
    costs = CostModel(min_commission=0, slippage_bps=0, max_participation=1.0)
    r = run(data, lambda ctx: {"A": 1.0} if ctx.date < DAYS[2] else {}, dt.date(2026, 1, 5),
            dt.date(2026, 1, 9), 100_000, "D", costs)
    sell = r.trades[r.trades["side"] == "sell"].iloc[0]
    assert sell["tax"] == pytest.approx(sell["value"] * 0.003)
    expected = 100_000 - r.trades["commission"].sum() - sell["tax"]
    assert r.equity.iloc[-1] == pytest.approx(expected)


def test_whole_lot_rounding():
    data = _data({"A": [600] * 5})
    costs = CostModel(commission_rate=0, min_commission=0, slippage_bps=0, lot_size=1000,
                      max_participation=1.0)
    r = run(data, _hold("A"), dt.date(2026, 1, 5), dt.date(2026, 1, 9), 1_000_000, "once", costs)
    assert r.trades.iloc[0]["shares"] == 1000  # 100 萬只夠買 1 張 600 元的股票
    assert r.cash.iloc[-1] == pytest.approx(400_000)


def test_suspended_then_delisted_frozen_until_delisting_date():
    # 1/7 起停止交易，1/9 終止上市：期間資金卡住、賣不掉，終止日以最後收盤價的 50% 轉現金
    data = _data({"A": [10, 10, None, None, None], "B": [10] * 5},
                 sec={"A": {"status": "delisted", "last_date": dt.date(2026, 1, 6),
                            "delisted_date": dt.date(2026, 1, 9)}})
    costs = CostModel(commission_rate=0, min_commission=0, tax_stock=0, slippage_bps=0, max_participation=1.0,
                      delist_recovery=0.5)

    def strategy(ctx):
        return {"A": 1.0} if ctx.date == DAYS[0] else {"B": 1.0}
    r = run(data, strategy, dt.date(2026, 1, 5), dt.date(2026, 1, 9), 1000, "D", costs)
    assert r.equity.loc["2026-01-08"] == pytest.approx(1000)  # 凍結，以最後收盤價估值
    assert any("無成交，賣單未執行" in e for e in r.events)
    assert r.equity.iloc[-1] == pytest.approx(500)


def test_weights_above_one_rejected():
    data = _data({"A": [10] * 5, "B": [10] * 5})
    with pytest.raises(ValueError):
        run(data, lambda ctx: {"A": 0.7, "B": 0.7}, dt.date(2026, 1, 5), dt.date(2026, 1, 9), 1000, "once")


def test_context_history_cannot_see_future():
    data = _data({"A": [1, 2, 3, 4, 5]})
    seen = []

    def strategy(ctx):
        seen.append((ctx.date, ctx.history("close")["A"].iloc[-1]))
        return {}
    run(data, strategy, dt.date(2026, 1, 5), dt.date(2026, 1, 9), 1000, "D", NO_COST)
    assert all(px == data["close"].loc[d, "A"] for d, px in seen)


def test_rebalance_dates_monthly():
    days = pd.bdate_range("2026-01-01", "2026-03-31")
    expected = pd.to_datetime(["2026-01-30", "2026-02-27", "2026-03-31"])
    assert sorted(rebalance_dates(days, "M")) == list(expected)


def test_metrics():
    eq = pd.Series([100, 110, 99, 121], index=pd.to_datetime(["2025-01-01", "2025-07-01", "2026-01-01",
                                                               "2026-12-31"]), dtype=float)
    m = summarize(eq)
    assert m["total_return"] == pytest.approx(0.21)
    assert m["max_drawdown"] == pytest.approx(-0.1)
    yr = yearly_returns(eq)
    assert yr.loc[2025, "strategy"] == pytest.approx(0.10)


def test_point_in_time_hides_unpublished_revenue():
    db = Database()
    rows = [Revenue(2026, m, "TWSE", "1111", "x", None, 100 * m, 1, 1, 1, None, dt.date(2026, m + 1, 11))
            for m in (1, 2)]
    db.replace_revenue("s", "x", 2026, 1, rows[:1], True)
    db.replace_revenue("s", "y", 2026, 2, rows[1:], True)
    pit = PointInTime(db, dt.date(2026, 12, 31))
    assert pit.latest_revenue(pd.Timestamp("2026-03-10"))["month"].tolist() == [1]  # 2 月營收 3/11 才可用
    assert pit.latest_revenue(pd.Timestamp("2026-03-11"))["month"].tolist() == [2]
    db.close()
