"""Phase 3 機器學習選股（docs/研究報告/機器學習研究計畫.md）。

流程：
1. `build_dataset`：每月最後一個交易日（訊號日）取可投資範圍的股票，特徵做當日橫斷面百分位排名，
   標籤為訊號日次一交易日開盤起持有 h 日的超額報酬（相對加權報酬指數）及其橫斷面排名。
2. `walk_forward`：每年年初用「標籤在當年第一個交易日之前已完全實現」的樣本重新訓練（purge），
   預測當年各訊號日的分數。測試年份的資料不會進入訓練。
3. `MLStrategy`：在訊號日依分數選前 n 檔（沿用 Phase 2 的緩衝規則）。

標籤只用來訓練與評估，策略在訊號日只讀當天的預測分數。
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from apex.backtest.data import MarketData
from apex.backtest.engine import Context, rebalance_dates
from apex.backtest.strategies import liquid_universe
from apex.research.features import Features
from apex.research.regime import index_series, market_regime

# 個股特徵（做當日橫斷面百分位排名）。事先列好，不依結果增減。
STOCK_FEATURES = [
    "ret_5", "ret_20", "ret_60", "ret_120", "mom_240_20",
    "ma_gap_20", "ma_gap_60", "ma_gap_240", "ma_slope_60", "ma_slope_120",
    "high_gap_240", "vol_20", "vol_60", "downvol_60", "mdd_120", "gap_20",
    "value_20", "value_ratio_5_60",
    "ind_ret_60", "ind_rel_60", "ind_rel_120",
    "foreign_20", "foreign_60", "trust_20", "trust_60", "dealer_20", "margin_chg_20", "short_ratio",
    "rev_yoy", "rev_yoy_3m", "rev_yoy_12m", "rev_accel", "rev_up_streak",
    "eps", "eps_yoy", "gross_margin", "op_margin", "gm_chg", "om_chg", "roe_ttm", "debt_ratio",
    "mcap",
]
# 市場特徵（原值，同一天所有股票相同；讓模型依市場狀態調整）
MARKET_FEATURES = ["mkt_ma_gap", "mkt_ma_slope", "mkt_vol_pct", "mkt_breadth"]
FEATURES = STOCK_FEATURES + MARKET_FEATURES
HORIZONS = (20, 60, 120)


@dataclass
class Dataset:
    """樣本表：index 為 (date, code)；X 為特徵，y 含 fwd_ret、excess、label（超額排名）、label_end。"""

    X: pd.DataFrame
    y: pd.DataFrame
    horizon: int


def _cs_rank(df: pd.DataFrame) -> pd.DataFrame:
    return df.rank(axis=1, pct=True)


def build_dataset(f: Features, horizon: int = 60, dates: list[pd.Timestamp] | None = None,
                  universe: Callable[[Context], pd.Index] = liquid_universe,
                  stock_features: list[str] | None = None, regime: pd.DataFrame | None = None,
                  tr: pd.Series | None = None) -> Dataset:
    """建立月頻樣本。dates 預設為資料內每月最後一個交易日（扣除前 240 日暖機）。

    標籤：訊號日 t 的下一個交易日 t+1 開盤買進，持有到 t+1+h 開盤（與回測成交方式一致）；
    超額報酬＝個股還原開盤價報酬 − 同期加權報酬指數收盤報酬（t → t+h）。期末之後沒有資料的樣本標籤為 NaN。
    下市或停止交易的股票以最後成交價計（與回測的下市處理相近，偏樂觀）。
    """
    data = f.data
    days = data.days
    if dates is None:
        dates = sorted(d for d in rebalance_dates(days, "M") if days.get_loc(d) >= 240)
    pos = {d: i for i, d in enumerate(days)}

    # 個股特徵：先取所需日期的列再做橫斷面排名，避免整個面板排名
    names = stock_features or STOCK_FEATURES
    feats = {name: f[name].reindex(index=dates) for name in names}
    reg = (regime if regime is not None else market_regime(data)).reindex(dates)
    tr = (tr if tr is not None else index_series(data, "TAIEX_TR")).reindex(days).ffill()
    ao = data["adj_open"].ffill()

    rows_X, rows_y = [], []
    for d in dates:
        i = pos[d]
        u = universe(Context(d, data, i))
        if len(u) == 0:
            continue
        X = pd.DataFrame({name: feats[name].loc[d].reindex(u) for name in names})
        X = X.rank(pct=True).astype("float32")
        X["mkt_ma_gap"] = np.float32(reg.at[d, "ma_gap"])
        X["mkt_ma_slope"] = np.float32(reg.at[d, "ma_slope"])
        X["mkt_vol_pct"] = np.float32(reg.at[d, "vol_pct"])
        X["mkt_breadth"] = np.float32(reg.at[d, "breadth"])
        X.index = pd.MultiIndex.from_product([[d], X.index], names=["date", "code"])

        y = pd.DataFrame(index=X.index, columns=["fwd_ret", "excess", "label", "label_end"], dtype="float64")
        j0, j1 = i + 1, i + 1 + horizon
        if j1 < len(days):
            buy, sell = ao.iloc[j0].reindex(u), ao.iloc[j1].reindex(u)
            ret = (sell / buy - 1).to_numpy()
            mkt = float(tr.iloc[i + horizon] / tr.iloc[i] - 1)
            y["fwd_ret"] = ret
            y["excess"] = ret - mkt
            y["label"] = pd.Series(ret).rank(pct=True).to_numpy()
            y["label_end"] = j1
        rows_X.append(X)
        rows_y.append(y)
    X = pd.concat(rows_X)
    y = pd.concat(rows_y)
    y["label_end"] = y["label_end"].map(lambda v: days[int(v)] if pd.notna(v) else pd.NaT)
    return Dataset(X[names + MARKET_FEATURES], y, horizon)


# ── 模型 ────────────────────────────────────────────────────────────────

TARGETS = ("rank", "up")  # rank：超額報酬排名（迴歸）；up：持有期間報酬是否為正（分類，輸出機率）


def make_model(name: str, seed: int = 0, target: str = "rank"):
    """事先固定的超參數（不在研究期反覆調整，避免過度擬合）。"""
    if target == "up":
        return _make_classifier(name, seed)
    if name == "ridge":
        from sklearn.linear_model import Ridge
        return _FillNa(Ridge(alpha=1.0))
    if name == "lgbm":
        import lightgbm as lgb
        return lgb.LGBMRegressor(n_estimators=400, learning_rate=0.03, num_leaves=31, min_child_samples=200,
                                 subsample=0.8, subsample_freq=1, colsample_bytree=0.7, reg_lambda=1.0,
                                 random_state=seed, verbose=-1)
    if name == "xgb":
        import xgboost as xgb
        return xgb.XGBRegressor(n_estimators=400, learning_rate=0.03, max_depth=5, min_child_weight=200,
                                subsample=0.8, colsample_bytree=0.7, reg_lambda=1.0, tree_method="hist",
                                device=_xgb_device(), random_state=seed)
    raise ValueError(f"未知模型 {name}，可用：ridge、lgbm、xgb")


def _make_classifier(name: str, seed: int):
    if name == "ridge":  # 分類任務的線性基準為 Logistic Regression
        from sklearn.linear_model import LogisticRegression
        return _FillNa(LogisticRegression(C=1.0, max_iter=1000))
    if name == "lgbm":
        import lightgbm as lgb
        return lgb.LGBMClassifier(n_estimators=400, learning_rate=0.03, num_leaves=31, min_child_samples=200,
                                  subsample=0.8, subsample_freq=1, colsample_bytree=0.7, reg_lambda=1.0,
                                  random_state=seed, verbose=-1)
    if name == "xgb":
        import xgboost as xgb
        return xgb.XGBClassifier(n_estimators=400, learning_rate=0.03, max_depth=5, min_child_weight=200,
                                 subsample=0.8, colsample_bytree=0.7, reg_lambda=1.0, tree_method="hist",
                                 device=_xgb_device(), random_state=seed)
    raise ValueError(f"未知模型 {name}，可用：ridge、lgbm、xgb")


_DEVICE: str | None = None


def _xgb_device() -> str:
    """有 CUDA GPU 就用 GPU，否則 CPU。"""
    global _DEVICE
    if _DEVICE is None:
        try:
            import xgboost as xgb
            xgb.XGBRegressor(n_estimators=1, device="cuda").fit(np.zeros((4, 1)), np.zeros(4))
            _DEVICE = "cuda"
        except Exception:
            _DEVICE = "cpu"
    return _DEVICE


class _FillNa:
    """線性模型不接受缺值：排名特徵缺值補 0.5（中位數），市場特徵補 0。"""

    def __init__(self, model):
        self.model = model

    @staticmethod
    def _fill(X: pd.DataFrame) -> pd.DataFrame:
        X = X.copy()
        cols = [c for c in X.columns if c not in MARKET_FEATURES]
        X[cols] = X[cols].fillna(0.5)
        return X.fillna(0.0)

    def fit(self, X, y):
        self.model.fit(self._fill(X), y)
        return self

    def predict(self, X):
        return self.model.predict(self._fill(X))

    def predict_proba(self, X):
        return self.model.predict_proba(self._fill(X))


# ── 滾動式檢查 ──────────────────────────────────────────────────────────

@dataclass
class Fold:
    year: int
    train_rows: int
    train_end: pd.Timestamp  # 訓練樣本中最晚的標籤實現日
    seconds: float


def target_values(ds: Dataset, target: str) -> pd.Series:
    """訓練目標：rank＝超額報酬的橫斷面排名；up＝持有期間報酬 > 0（1／0，未實現為 NaN）。"""
    if target == "rank":
        return ds.y["label"]
    if target == "up":
        return (ds.y["fwd_ret"] > 0).astype(float).where(ds.y["fwd_ret"].notna())
    raise ValueError(f"未知目標 {target}，可用：{', '.join(TARGETS)}")


def walk_forward(ds: Dataset, model: str, test_years: list[int], seed: int = 0,
                 min_train_years: int = 2, target: str = "rank") -> tuple[pd.Series, list[Fold]]:
    """每年重新訓練一次。訓練樣本必須在測試年第一個交易日之前就已實現標籤（purge），測試年只預測。

    target='up' 時輸出上漲機率（0～1）。
    """
    yv = target_values(ds, target)
    dates = ds.X.index.get_level_values("date")
    preds, folds = [], []
    for year in test_years:
        test_mask = dates.year == year
        if not test_mask.any():
            continue
        first = dates[test_mask].min()
        train_mask = (ds.y["label_end"] < first).to_numpy() & yv.notna().to_numpy()
        train_dates = dates[train_mask]
        if not len(train_dates) or (first - train_dates.min()).days < 365 * min_train_years - 30:
            continue
        t0 = time.time()
        m = make_model(model, seed, target)
        m.fit(ds.X[train_mask], yv[train_mask].to_numpy())
        Xt = ds.X[test_mask]
        raw = m.predict_proba(Xt)[:, 1] if target == "up" else m.predict(Xt)
        p = pd.Series(raw, index=ds.X.index[test_mask], name="score")
        preds.append(p)
        folds.append(Fold(year, int(train_mask.sum()), ds.y["label_end"][train_mask].max(), time.time() - t0))
    return (pd.concat(preds) if preds else pd.Series(dtype=float, name="score")), folds


def evaluate_predictions(pred: pd.Series, y: pd.DataFrame, top: float = 0.1,
                         min_names: int = 20) -> pd.DataFrame:
    """逐年：Rank IC（分數與實際超額報酬的 Spearman 相關）平均、IC 為正的月份比例、
    前 top 比例股票的平均超額報酬減全體平均（單期 h 日，不年化）。"""
    df = pd.DataFrame({"score": pred}).join(y[["excess"]]).dropna()
    rows = []
    for d, g in df.groupby(level="date"):
        if len(g) < min_names:
            continue
        ic = g["score"].rank().corr(g["excess"].rank())
        k = max(1, int(len(g) * top))
        spread = g.nlargest(k, "score")["excess"].mean() - g["excess"].mean()
        rows.append((d, ic, spread))
    per = pd.DataFrame(rows, columns=["date", "ic", "spread"]).set_index("date")
    if per.empty:
        return per
    out = per.groupby(per.index.year).agg(ic=("ic", "mean"), ic_pos=("ic", lambda s: (s > 0).mean()),
                                          spread=("spread", "mean"), months=("ic", "size"))
    total = pd.DataFrame({"ic": [per["ic"].mean()], "ic_pos": [(per["ic"] > 0).mean()],
                          "spread": [per["spread"].mean()], "months": [len(per)]}, index=["全期"])
    out.index = out.index.astype(str)
    return pd.concat([out, total])


def calibration(prob: pd.Series, y: pd.DataFrame, bins: tuple[float, ...] = (0, .3, .4, .5, .6, .7, 1)
                ) -> pd.DataFrame:
    """機率校準：依預測上漲機率分組，比較平均預測機率與實際上漲比例、平均報酬。"""
    df = pd.DataFrame({"prob": prob}).join(y[["fwd_ret"]]).dropna()
    df["bin"] = pd.cut(df["prob"], list(bins), include_lowest=True)
    g = df.groupby("bin", observed=True)
    hit = g["fwd_ret"].apply(lambda r: (r > 0).mean())
    return pd.DataFrame({"預測機率": g["prob"].mean(), "實際上漲比例": hit,
                         "平均報酬": g["fwd_ret"].mean(), "樣本數": g.size()})


# ── 策略 ────────────────────────────────────────────────────────────────

class MLStrategy:
    """訊號日依模型分數選前 n 檔、等權重；現有持股仍在前 buffer × n 名就保留。沒有分數的日子不持股。

    top_k 不為 None 時，只從訊號日市值前 top_k 大的股票中挑選（模型訓練仍用全部可投資股票）。
    min_score 不為 None 時（上漲機率模型），只買分數 ≥ min_score 的股票，每檔固定 1/n，其餘保留現金；
    符合的股票少，持股就少，可以完全空手。
    """

    def __init__(self, scores: pd.Series, n: int = 15, buffer: float = 3.0, mcap: pd.DataFrame | None = None,
                 top_k: int | None = None, min_score: float | None = None):
        from apex.research.strategies import pick
        self._pick = pick
        self.scores = scores
        self.by_date = {d: g.droplevel("date") for d, g in scores.groupby(level="date")}
        self.n, self.buffer = n, buffer
        self.mcap, self.top_k = mcap, top_k
        self.min_score = min_score
        self.held: list[str] = []

    def __call__(self, ctx: Context) -> dict[str, float]:
        s = self.by_date.get(ctx.date)
        if s is None:
            return {}
        if self.top_k:
            big = self.mcap.loc[ctx.date].reindex(s.index).nlargest(self.top_k).index
            s = s[s.index.isin(big)]
        if self.min_score is not None:
            s = s[s >= self.min_score]
            self.held = self._pick(s, self.n, self.held, self.buffer)
            return {c: 1 / self.n for c in self.held}
        self.held = self._pick(s, self.n, self.held, self.buffer)
        return {c: 1 / len(self.held) for c in self.held} if self.held else {}


ML_START = dt.date(2016, 1, 4)  # 訓練資料起點（研究期第一天；之前為特徵暖機）


def scores_for(db, end: dt.date, model: str, horizon: int, cache: dict | None = None,
               target: str = "rank") -> pd.Series:
    """從 ML_START 到 end 建資料集，對 ML_START+2 年起每一年做滾動式預測，回傳全部分數。

    end 必須已通過期間檢查（保留期鎖定由呼叫端負責）。cache 以 (end, horizon) 存資料集。
    """
    from apex.backtest import load
    key = (end, horizon)
    if cache is not None and key in cache:
        ds = cache[key]
    else:
        data: MarketData = load(db, ML_START, end)
        ds = build_dataset(Features(data), horizon)
        if cache is not None:
            cache[key] = ds
    years = sorted(set(ds.X.index.get_level_values("date").year))
    pred, _ = walk_forward(ds, model, [y for y in years if y >= ML_START.year + 2], target=target)
    return pred
