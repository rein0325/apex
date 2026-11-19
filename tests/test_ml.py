import numpy as np
import pandas as pd
import pytest

from apex.research.features import Features
from apex.research.ml import MARKET_FEATURES, MLStrategy, build_dataset, evaluate_predictions, walk_forward
from tests.test_research import _random_data, _truncate

FEATS = ["ret_20", "ma_gap_60", "vol_20"]


def _regime(days):
    return pd.DataFrame({"ma_gap": 0.01, "ma_slope": 0.0, "vol_pct": 0.5, "breadth": 0.6}, index=days)


def _ds(data, horizon=20, dates=None):
    tr = pd.Series(np.linspace(100, 120, len(data.days)), index=data.days)
    return build_dataset(Features(data), horizon, dates=dates, universe=lambda ctx: ctx.tradable(),
                         stock_features=FEATS, regime=_regime(data.days), tr=tr), tr


def test_dataset_label_matches_next_open_execution():
    data = _random_data(n_days=600, n_codes=8)
    ds, tr = _ds(data)
    d = ds.X.index.get_level_values("date")[0]
    i = data.days.get_loc(d)
    code = "1103"
    ao = data["adj_open"]
    ret = ao.iloc[i + 21][code] / ao.iloc[i + 1][code] - 1
    row = ds.y.loc[(d, code)]
    assert row["fwd_ret"] == pytest.approx(ret)
    assert row["excess"] == pytest.approx(ret - (tr.iloc[i + 20] / tr.iloc[i] - 1))
    assert row["label_end"] == data.days[i + 21]
    assert list(ds.X.columns) == FEATS + MARKET_FEATURES
    assert ds.X[FEATS].max().max() <= 1  # 橫斷面百分位
    # 最後幾個月標籤尚未實現
    assert ds.y["label"].isna().any() and ds.y["label"].notna().any()


def test_dataset_features_have_no_lookahead():
    """在訊號日截斷資料，特徵必須相同（標籤可以不同，因為標籤本來就是未來）。"""
    data = _random_data(n_days=600, n_codes=8)
    full, _ = _ds(data)
    d = full.X.index.get_level_values("date").unique()[5]
    i = data.days.get_loc(d)
    part, _ = _ds(_truncate(data, i), dates=[d])
    pd.testing.assert_frame_equal(full.X.loc[[d]], part.X.loc[[d]])


def test_walk_forward_purges_overlapping_labels():
    data = _random_data(n_days=1000, n_codes=10)
    ds, _ = _ds(data, horizon=60)
    years = sorted(set(ds.X.index.get_level_values("date").year))
    pred, folds = walk_forward(ds, "ridge", years, min_train_years=1)
    assert folds, "至少要有一個測試年"
    for fold in folds:
        dates = pred.index.get_level_values("date")
        first = dates[dates.year == fold.year].min()
        assert fold.train_end < first
    assert set(pred.index.get_level_values("date").year) == {f.year for f in folds}
    ev = evaluate_predictions(pred, ds.y, min_names=5)
    assert "全期" in ev.index and ev.loc["全期", "ic"] == pytest.approx(ev.loc["全期", "ic"])


def test_ml_strategy_picks_top_scores_with_buffer():
    d1, d2 = pd.Timestamp("2024-01-31"), pd.Timestamp("2024-02-29")
    idx = pd.MultiIndex.from_product([[d1, d2], list("ABCDEF")], names=["date", "code"])
    scores = pd.Series([6, 5, 4, 3, 2, 1, 1, 2, 6, 5, 4, 3], index=idx, dtype=float)
    s = MLStrategy(scores, n=2, buffer=2)

    class Ctx:
        def __init__(self, d):
            self.date = d
    assert s(Ctx(d1)) == {"A": 0.5, "B": 0.5}
    w = s(Ctx(d2))  # 2 月 A、B 排名 6、5 → 不在前 4 → 換成 C、D
    assert set(w) == {"C", "D"}
    assert s(Ctx(pd.Timestamp("2024-03-29"))) == {}


def test_ml_strategy_restricts_to_largest_caps():
    d = pd.Timestamp("2024-01-31")
    idx = pd.MultiIndex.from_product([[d], list("ABCD")], names=["date", "code"])
    scores = pd.Series([4, 3, 2, 1], index=idx, dtype=float)
    mcap = pd.DataFrame({"A": [1.0], "B": [2.0], "C": [30.0], "D": [40.0]}, index=[d])
    s = MLStrategy(scores, n=1, buffer=1, mcap=mcap, top_k=2)

    class Ctx:
        date = d
    assert s(Ctx()) == {"C": 1.0}


def test_up_target_and_cash_when_few_qualify():
    from apex.research.ml import calibration, target_values
    data = _random_data(n_days=1000, n_codes=10)
    ds, _ = _ds(data, horizon=60)
    up = target_values(ds, "up")
    assert set(up.dropna().unique()) <= {0.0, 1.0}
    assert up.isna().equals(ds.y["fwd_ret"].isna())
    years = sorted(set(ds.X.index.get_level_values("date").year))
    prob, folds = walk_forward(ds, "ridge", years, min_train_years=1, target="up")
    assert folds and prob.between(0, 1).all()
    cal = calibration(prob, ds.y)
    assert cal["樣本數"].sum() == ds.y.loc[prob.index, "fwd_ret"].notna().sum()

    d = pd.Timestamp("2024-01-31")
    idx = pd.MultiIndex.from_product([[d], list("ABCD")], names=["date", "code"])
    s = MLStrategy(pd.Series([0.8, 0.65, 0.55, 0.3], index=idx), n=4, buffer=1, min_score=0.6)

    class Ctx:
        date = d
    assert s(Ctx()) == {"A": 0.25, "B": 0.25}  # 只有 2 檔達標，其餘 50% 放現金
