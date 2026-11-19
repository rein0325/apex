import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from apex.backtest.engine import Context, rebalance_dates
from apex.db import Database
from apex.research import trials
from apex.research.lab import random_strategy, replacement_rate
from apex.research.periods import HoldoutLocked, check_range, period_range
from tests.test_research import _random_data


def test_holdout_is_locked_by_default():
    last = dt.date(2026, 11, 17)
    assert period_range("research", last) == (dt.date(2016, 1, 4), dt.date(2022, 12, 30))
    with pytest.raises(HoldoutLocked):
        period_range("holdout", last)
    assert period_range("holdout", last, unlock_holdout=True) == (dt.date(2025, 1, 2), dt.date(2026, 10, 30))
    with pytest.raises(HoldoutLocked):
        period_range("live", last)
    with pytest.raises(HoldoutLocked):
        check_range(dt.date(2020, 1, 1), dt.date(2025, 3, 1))
    check_range(dt.date(2020, 1, 1), dt.date(2024, 12, 31))


def test_biweekly_rebalance_dates():
    days = pd.bdate_range("2024-01-01", "2024-02-29")
    weekly = sorted(rebalance_dates(days, "W"))
    biweekly = sorted(rebalance_dates(days, "2W"))
    assert biweekly == weekly[::2]
    assert biweekly[0] == pd.Timestamp("2024-01-05")


def test_replacement_rate():
    t = {pd.Timestamp("2024-01-31"): {"A": .5, "B": .5}, pd.Timestamp("2024-02-29"): {"A": .5, "C": .5},
         pd.Timestamp("2024-03-29"): {"A": .5, "C": .5}}
    assert replacement_rate(t) == pytest.approx(0.25)  # 50%、0%


def test_random_strategy_is_reproducible_and_respects_replace():
    data = _random_data(n_days=200, n_codes=12)
    data.panels["value"] = data.panels["value"] * 0 + 1e9  # 全部通過流動性篩選
    data.panels["close"] = data.panels["close"] * 0 + 50
    days = [150, 170, 190]

    def picks(seed, replace):
        s = random_strategy(4, replace, seed)
        return [set(s(Context(data.days[i], data, i))) for i in days]

    a, b = picks(7, 0.5), picks(7, 0.5)
    assert a == b
    assert all(len(x) == 4 for x in a)
    assert len(a[1] - a[0]) == 2  # 換掉一半
    still = picks(3, 0.0)
    assert still[0] == still[1] == still[2]


def test_expected_max_sharpe_grows_with_trials():
    v = 0.0004
    assert trials.expected_max_sr(1, v) == 0
    assert 0 < trials.expected_max_sr(10, v) < trials.expected_max_sr(100, v)


def test_deflated_sharpe_single_trial_is_psr():
    sr, n = 0.05, 1000
    dsr = trials.deflated_sharpe(sr, n, 0.0, 3.0, 1, 0.0)
    assert dsr == pytest.approx(0.5 * (1 + math.erf(sr * math.sqrt(n - 1) / math.sqrt(1 + sr ** 2 / 2)
                                                     / math.sqrt(2))))
    assert trials.deflated_sharpe(sr, n, 0.0, 3.0, 200, 0.001) < dsr  # 試越多次，可信度越低


def test_record_and_deflate_roundtrip():
    db = Database()
    rng = np.random.default_rng(0)
    days = pd.bdate_range("2020-01-01", periods=500)
    ids = []
    for i, mu in enumerate([0.0005, 0.0002, -0.0001]):
        eq = pd.Series(np.cumprod(1 + rng.normal(mu, 0.01, 500)), index=days)
        ids.append(trials.record(db, family="A", strategy=f"s:{i}", period="research", start=days[0].date(),
                                 end=days[-1].date(), freq="M", capital=1e5, costs={}, equity=eq,
                                 metrics={"cagr": 0.1, "sharpe": 1.0, "max_drawdown": -0.2,
                                          "x": float("nan")}))
    trials.record(db, family="base", strategy="equal:50", period="research", start=days[0].date(),
                  end=days[-1].date(), freq="M", capital=1e5, costs={}, metrics={"cagr": 0.0}, equity=eq)
    df = trials.trials(db, "research")
    assert len(df) == 4
    d = trials.deflate(db, ids[0])
    assert d["n_trials"] == 3  # 基準不計入
    assert 0 <= d["dsr"] <= 1


def test_pick_keeps_holdings_within_buffer():
    from apex.research.strategies import pick
    score = pd.Series({c: float(10 - i) for i, c in enumerate("ABCDEFGHIJ")})  # A 最高
    assert pick(score, 2, []) == ["A", "B"]
    assert pick(score, 2, ["D"], buffer=2) == ["D", "A"]  # D 排第 4，在前 4 名內 → 保留
    assert pick(score, 2, ["E"], buffer=2) == ["A", "B"]  # E 排第 5 → 換掉


def test_build_parses_params_and_overlays():
    from apex.research.features import Features
    from apex.research.strategies import _params, build, family_of
    assert _params("15:3") == (15, 3.0)
    assert _params("") == (10, 2.0)
    assert family_of("A1:15:3") == "A"
    assert family_of("A1:15:3+D3") == "D"
    assert family_of("equal:50") == "base"
    data = _random_data(n_days=300, n_codes=8)
    data.panels["value"] = data.panels["value"] * 0 + 1e9
    f = Features(data)
    s = build("buyhold:1101+D3", f)
    w = s(Context(data.days[280], data, 280))
    assert w == pytest.approx({"1101": 1.0})
    s = build("equal:4+D3", f)
    w = s(Context(data.days[280], data, 280))
    assert sum(w.values()) == pytest.approx(1.0) and sum(w.values()) <= 1 + 1e-9
    with pytest.raises(ValueError):
        build("equal:4+D9", f)


def test_core_satellite_weights_and_strip_core():
    from apex.research.features import Features
    from apex.research.lab import strip_core
    from apex.research.strategies import CoreSatellite, build, family_of
    assert family_of("CS:70&A1:15:3+D3") == "CS"
    data = _random_data(n_days=300, n_codes=8)
    data.panels["value"] = data.panels["value"] * 0 + 1e9
    f = Features(data)
    s = build("CS:70&equal:4", f)
    assert isinstance(s, CoreSatellite) and s.core == "0050" and s.core_regime is None
    w = s(Context(data.days[280], data, 280))
    assert w["0050"] == pytest.approx(0.7)
    sat = {c: v for c, v in w.items() if c != "0050"}
    assert len(sat) == 4 and sum(sat.values()) == pytest.approx(0.3)
    assert strip_core({data.days[280]: w}, "0050")[data.days[280]] == sat
    s2 = s.with_satellite(lambda ctx: {})
    assert s2(Context(data.days[280], data, 280)) == pytest.approx({"0050": 0.7})
    assert build("CS:60:D2:0.25&equal:4", f).core_regime == 0.25
    for bad in ("CS:70", "CS:70:D3&equal:4", "CS:150&equal:4"):
        with pytest.raises(ValueError):
            build(bad, f)


def test_random_table_migrates_old_schema(tmp_path):
    from apex.research.lab import ensure_random_table
    db = Database(tmp_path / "t.duckdb")
    db.con.execute("""CREATE TABLE research_random (n INTEGER, replace DOUBLE, freq VARCHAR, start_date DATE,
        end_date DATE, capital DOUBLE, costs VARCHAR, seed INTEGER, cagr DOUBLE, sharpe DOUBLE,
        max_drawdown DOUBLE, PRIMARY KEY (n, replace, freq, start_date, end_date, capital, costs, seed))""")
    db.con.execute("INSERT INTO research_random VALUES (5, 0.5, 'M', '2020-01-01', '2020-12-31', 1, '{}', 0, "
                   "0.1, 1.0, -0.2)")
    ensure_random_table(db)
    ensure_random_table(db)
    assert db.con.execute("SELECT variant, cagr FROM research_random").fetchall() == [("", 0.1)]
