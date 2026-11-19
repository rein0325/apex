import datetime as dt

import pandas as pd
import pytest

from apex.backtest.costs import CostModel
from apex.portfolio import TradeError, append_trade, broker_fee, compute, load_trades, sell_tax, text

DAYS = pd.bdate_range("2026-01-05", periods=6)
SEC = pd.DataFrame({"name": ["台泥", "元大台灣50"], "sec_type": ["stock", "etf"]}, index=["1101", "0050"])


def _prices():
    close = pd.DataFrame({"1101": [10, 11, 12, 10, 10, 11], "0050": [100, 100, 100, 100, 100, 100]},
                         index=DAYS, dtype=float)
    adj = close.copy()
    return close, adj


def _trades(rows):
    df = pd.DataFrame(rows, columns=["date", "code", "side", "shares", "price", "fee", "tax", "note"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def test_fees_follow_broker_rules():
    c = CostModel()
    assert broker_fee(500, c) == 1  # 最低 1 元
    assert broker_fee(100_000, c) == 142  # 142.5 無條件捨去
    assert sell_tax(100_000, "stock", "1101", c) == 300
    assert sell_tax(100_000, "etf", "0050", c) == 100
    assert sell_tax(100_000, "etf", "00679B", c) == 0


def test_average_cost_realized_and_drawdown():
    close, adj = _prices()
    t = _trades([
        ("2026-01-05", "1101", "buy", 1000, 10, 14, 0, ""),
        ("2026-01-07", "1101", "sell", 500, 12, 8, 18, ""),
    ])
    r = compute(t, close, adj, SEC)
    h = r.holdings[0]
    assert h.shares == pytest.approx(500)
    assert h.cost == pytest.approx(5007)  # (10,000 + 14) 的一半
    assert r.realized == pytest.approx(500 * 12 - 8 - 18 - 5007)
    assert h.value == pytest.approx(500 * 11)
    # 帳戶報酬：12 → 10 是從最高點跌 1/6
    assert r.max_drawdown == pytest.approx(10 / 12 - 1, rel=1e-6)
    assert "1101" in text(r)


def test_dividend_reinvested_and_split_shares():
    close, adj = _prices()
    # 第 4 天除息 1 元：之前的還原價 × (1 − 1/12)
    adj.loc[DAYS[:3], "1101"] = close.loc[DAYS[:3], "1101"] * (1 - 1 / 12)
    t = _trades([("2026-01-05", "1101", "buy", 1000, 10, 0, 0, "")])
    r = compute(t, close, adj, SEC)
    h = r.holdings[0]
    # 價值含股利再投入：1000 股 × 原始收盤 × 還原報酬
    assert h.value == pytest.approx(1000 * 10 * (11 / (10 * (1 - 1 / 12))))
    assert h.shares > 1000  # 約當股數因股利再投入而增加


def test_concentration_and_drawdown_warnings():
    close, adj = _prices()
    close["1101"] = [10, 10, 10, 7, 7, 7]
    adj = close.copy()
    t = _trades([("2026-01-05", "1101", "buy", 1000, 10, 0, 0, ""),
                 ("2026-01-05", "0050", "buy", 10, 100, 0, 0, "")])
    r = compute(t, close, adj, SEC)
    assert any("1101" in w and "占持股" in w for w in r.warnings)
    assert any("回撤" in w or "最高點低" in w for w in r.warnings)
    assert not any("0050" in w and "占持股" in w for w in r.warnings)  # ETF 不算集中


def test_selling_more_than_held_is_rejected():
    close, adj = _prices()
    t = _trades([("2026-01-05", "1101", "buy", 100, 10, 0, 0, ""),
                 ("2026-01-06", "1101", "sell", 200, 11, 0, 0, "")])
    with pytest.raises(TradeError):
        compute(t, close, adj, SEC)


def test_trade_file_roundtrip(tmp_path):
    p = tmp_path / "trades.csv"
    append_trade(p, dt.date(2026, 1, 5), "0050", "buy", 10, 100, 1, 0, "測試")
    append_trade(p, dt.date(2026, 1, 6), "0050", "sell", 5, 101, 1, 0)
    df = load_trades(p)
    assert list(df["side"]) == ["buy", "sell"] and df["code"].iloc[0] == "0050"
    p.write_text("date,code,side,shares,price\n2026-01-05,0050,hold,1,1\n", encoding="utf-8")
    with pytest.raises(TradeError):
        load_trades(p)
