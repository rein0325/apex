import numpy as np
import pandas as pd
import pytest
from cryptography.exceptions import InvalidTag

from apex import candidates, vault


def test_bucket_of_edges():
    assert candidates.bucket_of(0.99) == "前 2%"
    assert candidates.bucket_of(0.98) == "前 2～5%"
    assert candidates.bucket_of(0.5) == "後 50%"
    assert candidates.bucket_of(0.0) == "後 50%"


def test_calibration_uses_realized_returns_by_bucket():
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2020-01-31", periods=20, freq="BME")
    codes = [f"{1000 + i}" for i in range(100)]
    idx = pd.MultiIndex.from_product([dates, codes], names=["date", "code"])
    score = pd.Series(rng.normal(size=len(idx)), index=idx)
    # 分數越高，報酬越好
    y = pd.DataFrame({"fwd_ret": score * 0.05 + rng.normal(0, 0.05, len(idx))}, index=idx)
    y["excess"] = y["fwd_ret"] - 0.01
    vol = pd.Series(0.3, index=idx)
    t = candidates.calibration(score, y, vol, 60)
    assert t.loc["前 2%", "up"] > t.loc["全部", "up"] > t.loc["後 50%", "up"]
    assert t.loc["前 2%", "q50"] > t.loc["後 50%", "q50"]
    assert t.loc["全部", "n"] == len(idx)
    assert t.loc["前 2%", "beat"] > t.loc["後 50%", "beat"]
    assert t.loc["全部", "ex_q50"] == pytest.approx(t.loc["全部", "q50"] - 0.01)
    sd = 0.3 * np.sqrt(60 / 245)
    assert t.loc["全部", "z_q50"] == pytest.approx(t.loc["全部", "q50"] / sd)


def test_vault_roundtrip_and_wrong_password():
    obj = {"candidates": [{"code": "2330", "name": "台積電"}]}
    box = vault.encrypt(obj, "secret")
    assert "2330" not in str(box) and "台積電" not in str(box)
    assert vault.decrypt(box, "secret") == obj
    with pytest.raises(InvalidTag):
        vault.decrypt(box, "wrong")


def test_reasons_pick_top_contributors_with_plain_values():
    contrib = pd.Series({"rev_yoy_3m": 0.3, "eps": 0.2, "ma_gap_240": -0.4, "ret_20": 0.01,
                         "mkt_vol_pct": 0.9, "trust_20": -0.05})
    raw = {"rev_yoy_3m": 0.25, "eps": 3.2, "ma_gap_240": -0.12, "ret_20": 0.05, "trust_20": -0.01}
    r = candidates.reasons(contrib, raw)
    assert r["pros"][0] == "近 3 個月營收年增 +25%"
    assert r["pros"][1] == "最近一季 EPS 3.20 元"
    assert not any("mkt" in x for x in r["pros"] + r["cons"])  # 市場特徵不列入
    assert r["cons"][0].startswith("股價距年線 -12%")


def test_checks_and_advice_rules():
    good = {"ma_gap_20": .02, "ma_gap_60": .05, "ma_gap_240": .2, "ma_slope_60": .01, "ma_slope_120": .01,
            "rev_yoy_3m": .2, "rev_accel": .05, "eps_yoy": .3, "gm_chg": .01, "foreign_20": .02,
            "trust_20": 0}
    assert all(c["ok"] for c in candidates.checks(good))
    missing = candidates.checks({**good, "eps_yoy": None})
    assert [c["ok"] for c in missing if c["name"] == "獲利改善"] == [None]
    assert candidates.advice(0.8, good)["level"] == "續抱"
    assert candidates.advice(0.3, good)["level"] == "留意"
    assert candidates.advice(0.8, {**good, "ma_gap_60": -0.01})["level"] == "留意"
    assert candidates.advice(0.1, {**good, "ma_gap_60": -0.01})["level"] == "考慮減碼"


def test_track_outcomes_matured_and_running():
    from apex.track import outcomes
    days = pd.bdate_range("2026-01-05", periods=10)
    opn = pd.DataFrame({"A": [10.0] * 10, "0050": [100.0] * 10}, index=days)
    opn.loc[days[5]:, "A"] = 12.0
    cls = opn + 0.5
    log = pd.DataFrame({"signal_date": [days[0].date(), days[7].date()], "code": ["A", "A"], "rank": [1, 1],
                        "up": [0.56, 0.56], "q50": [0.02, 0.02]})
    out = outcomes(log, opn, cls, horizon=3).set_index("signal_date")
    first = out.loc[days[0].date()]
    # days[1] 開盤買、days[4] 開盤賣
    assert first["matured"] and first["days"] == 3 and first["ret"] == pytest.approx(0.0)
    second = out.loc[days[7].date()]
    assert not second["matured"] and second["ret"] == pytest.approx(12.5 / 12 - 1)  # 開 days[8]，算到最新收盤


def _matured(excess: float, months: int = 8, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for d in pd.date_range("2027-01-05", periods=months, freq="MS") + pd.Timedelta(days=4):
        for k in range(10):
            rows.append({"signal_date": d.date(), "code": f"{1101 + k}", "rank": k + 1, "matured": True,
                         "days": 60, "ret": 0.10 + excess + rng.normal(0, 0.01), "bench": 0.10, "up": 0.6,
                         "q50": 0.05})
    return pd.DataFrame(rows)


def test_failure_alert_when_picks_lag_0050():
    from apex.track import summarize
    lag = summarize(_matured(-0.05))
    assert lag["health"]["status"] == "落後大盤"  # 上漲比例 100% 也照樣警示
    assert lag["matured"]["months"] == 8 and lag["matured"]["excess"] == pytest.approx(-0.05, abs=0.01)
    assert summarize(_matured(0.01))["health"]["status"] == "正常"
    assert summarize(_matured(-0.05, months=5))["health"]["status"] == "正常"  # 5 個月（50 筆）還不判斷落後
