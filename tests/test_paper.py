import datetime as dt

import pandas as pd
import pytest

from apex.db import Database
from apex.paper import ROUND_TRIP_COST, latest_signal_day, month_end_signal_days, periods

DAYS = pd.bdate_range("2026-01-26", periods=30)


def test_month_end_signal_days_use_calendar():
    db = Database()
    jan = [d.date() for d in pd.bdate_range("2026-01-01", "2026-01-28")]
    db.save_calendar_month(2026, 1, jan, False)  # 日曆只同步到 1/28
    db.replace_holidays(2026, [(dt.date(2026, 1, 30), "假設休市", True)])  # 公布休市 → 月底為 1/29
    ends = month_end_signal_days(db, dt.date(2026, 1, 1), dt.date(2026, 2, 28))
    assert ends[0] == dt.date(2026, 1, 29)
    assert ends[1] == dt.date(2026, 2, 27)  # 未同步的月份以平日推估
    assert latest_signal_day(db, dt.date(2026, 2, 10)) == dt.date(2026, 1, 29)
    assert latest_signal_day(db, dt.date(2026, 2, 27)) == dt.date(2026, 2, 27)


def test_periods_enter_next_open_and_chain():
    opn = pd.DataFrame({"A": range(100, 130), "0050": [50.0] * 30}, index=DAYS, dtype=float)
    cls = opn + 1
    d1, d2 = DAYS[3].date(), DAYS[10].date()
    weights = {d1: pd.Series({"A": 0.5}), d2: pd.Series(dtype=float)}
    late = pd.Series({d1: False, d2: True})
    p = periods([d1, d2], late, weights, opn, cls)
    # 第 1 期：DAYS[4] 開盤買、DAYS[11] 開盤賣；成本 = 換手 0.5 × 來回成本 ÷ 2
    expect = 0.5 * (111 / 104 - 1) - 0.5 * ROUND_TRIP_COST / 2
    assert p.loc[0, "return"] == pytest.approx(expect)
    assert p.loc[0, "done"] and p.loc[0, "bench"] == pytest.approx(0)
    # 第 2 期空手：只有賣出成本，最後一期算到最新收盤、標示進行中
    assert p.loc[1, "return"] == pytest.approx(-0.5 * ROUND_TRIP_COST / 2)
    assert not p.loc[1, "done"] and p.loc[1, "late"]
    assert p.loc[1, "cum"] == pytest.approx((1 + p.loc[0, "return"]) * (1 + p.loc[1, "return"]) - 1)
