"""Phase 2 候選策略登記處（docs/研究報告/策略研究計畫.md §5）。

策略規格字串格式：`名稱:參數`，例如 `equal:50`。研究策略需要特徵庫，所以由 `build(spec, features)`
建立；找不到時退回 Phase 1 的基準策略（apex.backtest.strategies）。
"""

from __future__ import annotations

import math
from collections.abc import Callable

import pandas as pd

from apex.backtest.engine import Context, Strategy, rebalance_dates
from apex.backtest.strategies import from_spec, liquid_universe
from apex.research.features import Features

# 名稱 → (類別, 建立函式(參數字串, 特徵庫))
REGISTRY: dict[str, tuple[str, Callable[[str, Features], Strategy]]] = {}
BASELINES = {"buyhold": "base", "equal": "base", "momentum": "base"}


def register(name: str, family: str):
    def deco(fn: Callable[[str, Features], Strategy]):
        REGISTRY[name] = (family, fn)
        return fn
    return deco


def family_of(spec: str) -> str:
    """有風險調整層的歸為 D（套在基準上也算 D，因為是在測試風險調整）；核心衛星歸為 CS。"""
    if spec.startswith("CS:"):
        return "CS"
    base, *overlays = spec.split("+")
    if overlays:
        return "D"
    name = base.partition(":")[0]
    return REGISTRY[name][0] if name in REGISTRY else BASELINES.get(name, "")


def build(spec: str, features: Features) -> Strategy:
    """'A1:15:3'；風險調整層以 + 串接，例如 'A1:15:3+D2'、'buyhold:0050+D1:20'。

    核心衛星：'CS:70&A1:15:3+D3' ＝ 70% 0050＋30% 衛星策略；'CS:70:D2&…' 的核心再套市場狀態調整。
    """
    if spec.startswith("CS:"):
        head, sep, sat = spec.partition("&")
        if not sep or not sat:
            raise ValueError(f"核心衛星格式應為 CS:核心%[:D2]&衛星策略，收到 {spec}")
        return core_satellite(head[3:], build(sat, features), features)
    base, *overlays = spec.split("+")
    name, _, arg = base.partition(":")
    strategy = REGISTRY[name][1](arg, features) if name in REGISTRY else from_spec(base)
    for ov in overlays:
        oname, _, oarg = ov.partition(":")
        if oname not in OVERLAYS:
            raise ValueError(f"未知的風險調整層 {oname}，可用：{', '.join(OVERLAYS)}")
        strategy = OVERLAYS[oname](strategy, oarg, features)
    return strategy

# ── 共用工具 ─────────────────────────────────────────────────────────────


def _rank(s: pd.Series) -> pd.Series:
    return s.rank(pct=True)


def pick(score: pd.Series, n: int, held: list[str], buffer: float = 2.0) -> list[str]:
    """依分數由高到低選 n 檔；現有持股只要仍在前 buffer × n 名就保留，降低換手。"""
    score = score.dropna().sort_values(ascending=False)
    if score.empty:
        return []
    top = list(score.index[:int(round(n * buffer))])
    keep = [c for c in held if c in top][:n]
    for c in score.index:
        if len(keep) >= n:
            break
        if c not in keep:
            keep.append(c)
    return keep


class Selector:
    """選股策略骨架：每次調整算出候選分數，經緩衝規則選出 n 檔，等權重。"""

    def __init__(self, f: Features, n: int, score_fn, buffer: float = 2.0):
        self.f, self.n, self.score_fn, self.buffer = f, n, score_fn, buffer
        self.held: list[str] = []

    def __call__(self, ctx: Context) -> dict[str, float]:
        universe = liquid_universe(ctx)
        if len(universe) == 0:
            return {}
        score = self.score_fn(self.f, ctx.date, universe)
        self.held = pick(score, self.n, self.held, self.buffer)
        return {c: 1 / len(self.held) for c in self.held} if self.held else {}


def _at(f: Features, name: str, d: pd.Timestamp, universe: pd.Index) -> pd.Series:
    return f.at(name, d).reindex(universe)


def _params(arg: str, default_n: int = 10, default_buffer: float = 2.0) -> tuple[int, float]:
    """'15' → (15, 2.0)；'15:3' → (15, 3.0)：持股檔數與緩衝倍數（現有持股在前 buffer × n 名內就保留）。"""
    parts = [p for p in arg.split(":") if p]
    n = int(parts[0]) if parts else default_n
    buffer = float(parts[1]) if len(parts) > 1 else default_buffer
    return n, buffer


def _selector(f: Features, arg: str, score) -> Selector:
    n, buffer = _params(arg)
    return Selector(f, n, score, buffer)


# ── A. 趨勢成長 ──────────────────────────────────────────────────────────

def _trend_ok(f: Features, d: pd.Timestamp, u: pd.Index) -> pd.Series:
    """股價在 60／120／240 日均線之上，且 60、120 日均線上揚。"""
    ok = pd.Series(True, index=u)
    for n in (60, 120, 240):
        ok &= _at(f, f"ma_gap_{n}", d, u) > 0
    for n in (60, 120):
        ok &= _at(f, f"ma_slope_{n}", d, u) > 0
    return ok


@register("A1", "A")
def trend_near_high(arg: str, f: Features) -> Strategy:
    """A1：多頭排列的股票中，最接近 52 週高點者。"""
    def score(f: Features, d: pd.Timestamp, u: pd.Index) -> pd.Series:
        return _at(f, "high_gap_240", d, u).where(_trend_ok(f, d, u))
    return _selector(f, arg, score)


@register("A2", "A")
def trend_strength(arg: str, f: Features) -> Strategy:
    """A2：多頭排列的股票中，接近 52 週高點與 120 日報酬兩項排名平均最高者。"""
    def score(f: Features, d: pd.Timestamp, u: pd.Index) -> pd.Series:
        s = (_rank(_at(f, "high_gap_240", d, u)) + _rank(_at(f, "ret_120", d, u))) / 2
        return s.where(_trend_ok(f, d, u))
    return _selector(f, arg, score)


# ── B. 強勢輪動 ──────────────────────────────────────────────────────────

@register("B1", "B")
def industry_rotation(arg: str, f: Features) -> Strategy:
    """B1：60 日動能最強的 3 個產業（至少 3 檔可投資股票），各產業內 60 日報酬最強者平均分配檔數。"""
    k = 3

    def score(f: Features, d: pd.Timestamp, u: pd.Index) -> pd.Series:
        ind = f.industry().loc[d].reindex(u)
        ind_ret = _at(f, "ind_ret_60", d, u)
        ret = _at(f, "ret_60", d, u)
        counts = ind.value_counts()
        ind_score = ind_ret.groupby(ind).first()
        ind_score = ind_score[counts.reindex(ind_score.index).fillna(0) >= 3]
        top = ind_score.nlargest(k).index
        # 產業排名決定組別，組內依報酬排名，輪流配置 → 分數＝組內名次為主、產業名次為輔
        within = ret.groupby(ind).rank(ascending=False)
        ind_pos = ind.map({x: i for i, x in enumerate(top)})
        s = -(within * k + ind_pos)
        return s.where(ind.isin(top) & ret.notna())
    return _selector(f, arg, score)


@register("B2", "B")
def residual_momentum(arg: str, f: Features) -> Strategy:
    """B2：相對同產業的 120 日超額報酬最高者，且股價在 60 日均線之上。"""
    def score(f: Features, d: pd.Timestamp, u: pd.Index) -> pd.Series:
        return _at(f, "ind_rel_120", d, u).where(_at(f, "ma_gap_60", d, u) > 0)
    return _selector(f, arg, score)


@register("B3", "B")
def strength_with_institutions(arg: str, f: Features) -> Strategy:
    """B3：60 日相對強度前 30%，且外資或投信近 20 日買超；依強度、外資、投信排名平均。"""
    def score(f: Features, d: pd.Timestamp, u: pd.Index) -> pd.Series:
        strength = _rank(_at(f, "ret_60", d, u))
        fo, tr = _at(f, "foreign_20", d, u), _at(f, "trust_20", d, u)
        s = (strength + _rank(fo) + _rank(tr)) / 3
        return s.where((strength >= 0.7) & ((fo > 0) | (tr > 0)))
    return _selector(f, arg, score)


# ── C. 成長潛力 ──────────────────────────────────────────────────────────

@register("C1", "C")
def revenue_acceleration(arg: str, f: Features) -> Strategy:
    """C1：近 3 月營收年增率高於近 12 月（加速）、近 3 月年增為正、單月年增連續 3 個月以上為正。"""
    def score(f: Features, d: pd.Timestamp, u: pd.Index) -> pd.Series:
        accel = _at(f, "rev_accel", d, u)
        ok = (accel > 0) & (_at(f, "rev_yoy_3m", d, u) > 0) & (_at(f, "rev_up_streak", d, u) >= 3)
        return accel.where(ok)
    return _selector(f, arg, score)


@register("C2", "C")
def earnings_improvement(arg: str, f: Features) -> Strategy:
    """C2：單季 EPS 年增且毛利率年增；依 EPS 年增、毛利率變化、ROE 排名平均。"""
    def score(f: Features, d: pd.Timestamp, u: pd.Index) -> pd.Series:
        eps, gm, roe = _at(f, "eps_yoy", d, u), _at(f, "gm_chg", d, u), _at(f, "roe_ttm", d, u)
        s = (_rank(eps) + _rank(gm) + _rank(roe)) / 3
        return s.where((eps > 0) & (gm > 0) & (roe > 0))
    return _selector(f, arg, score)


@register("C3", "C")
def growth_not_extended(arg: str, f: Features) -> Strategy:
    """C3：營收加速與獲利改善的綜合分數，排除股價高於 240 日均線 40% 以上（已過度反映）者。"""
    def score(f: Features, d: pd.Timestamp, u: pd.Index) -> pd.Series:
        accel, eps, gm = _at(f, "rev_accel", d, u), _at(f, "eps_yoy", d, u), _at(f, "gm_chg", d, u)
        s = (_rank(accel) + _rank(eps) + _rank(gm)) / 3
        ok = (_at(f, "rev_yoy_3m", d, u) > 0) & (eps > 0) & (_at(f, "ma_gap_240", d, u) < 0.4)
        return s.where(ok)
    return _selector(f, arg, score)


# ── ML. 機器學習分數（docs/研究報告/機器學習研究計畫.md）──────────────────────────────

_ML_SCORES: dict = {}
_ML_DATA: dict = {}


@register("ML", "ML")
def ml_rank(arg: str, f: Features) -> Strategy:
    """ML:模型:期間:檔數:緩衝[:市值前K大[:特徵組[:ic]]]，例如 ML:lgbm:60:15:3、ML:ridge:60:15:3:150。

    分數由滾動式訓練產生（每年重新訓練）；第 5 個參數限定只從市值前 K 大的股票中挑選。
    docs/研究報告/模型改進研究.md：
    - 模型、期間可用 / 串接表示投票，例如 ML:lgbm/xgb/ridge:20/60/120:6:3::v2。
    - 特徵組 v1（原本）或 v2（加估值）；第 8 個參數 ic ＝ 把握規則（最近 12 個月 IC ≤ 0 就改持 0050）。
    """
    from apex.research.ml import FEATURE_SETS, MLStrategy, dataset_for, trailing_ic, vote_scores
    parts = arg.split(":") + [""] * 8
    models = [m for m in parts[0].split("/") if m] or ["lgbm"]
    horizons = [int(h) for h in parts[1].split("/") if h] or [60]
    n, buffer = _params(":".join(parts[2:4]), default_n=15, default_buffer=3.0)
    top_k = int(parts[4]) if parts[4] else None
    features = parts[5] or "v1"
    if features not in FEATURE_SETS:
        raise ValueError(f"未知特徵組 {features}，可用：{', '.join(FEATURE_SETS)}")
    if parts[6] not in ("", "ic"):
        raise ValueError(f"第 8 個參數只能是 ic（把握規則），收到 {parts[6]}")
    end = f.days[-1].date()
    key = (tuple(models), tuple(horizons), features, end)
    if key not in _ML_SCORES:
        _ML_SCORES[key] = vote_scores(f.db, end, models, horizons, cache=_ML_DATA, features=features)
    scores = _ML_SCORES[key]
    gate = None
    if parts[6] == "ic":
        gate = trailing_ic(scores, dataset_for(f.db, end, 60, _ML_DATA, features).y)
    return MLStrategy(scores, n, buffer, f["mcap"] if top_k else None, top_k, gate=gate)


@register("MLP", "ML")
def ml_up_prob(arg: str, f: Features) -> Strategy:
    """MLP:模型:期間:檔數:緩衝:最低機率，例如 MLP:lgbm:60:15:3:0.6。

    模型預測持有期間上漲的機率；只買機率 ≥ 最低機率的前 n 檔，每檔 1/n，其餘放現金（可以空手）。
    """
    from apex.research.ml import MLStrategy, scores_for
    parts = arg.split(":")
    model = parts[0] if parts and parts[0] else "lgbm"
    horizon = int(parts[1]) if len(parts) > 1 and parts[1] else 60
    n, buffer = _params(":".join(parts[2:4]), default_n=15, default_buffer=3.0)
    min_prob = float(parts[4]) if len(parts) > 4 and parts[4] else 0.5
    end = f.days[-1].date()
    key = ("up", model, horizon, end)
    if key not in _ML_SCORES:
        _ML_SCORES[key] = scores_for(f.db, end, model, horizon, cache=_ML_DATA, target="up")
    return MLStrategy(_ML_SCORES[key], n, buffer, min_score=min_prob)


# ── D. 風險調整層 ────────────────────────────────────────────────────────

def _regime(f: Features) -> pd.DataFrame:
    if "_regime" not in f._cache:
        from apex.research.regime import market_regime
        f._cache["_regime"] = market_regime(f.data)
    return f._cache["_regime"]


def _scale(weights: dict[str, float], k: float) -> dict[str, float]:
    return {c: w * k for c, w in weights.items()}


def regime_exposure(strategy: Strategy, arg: str, f: Features) -> Strategy:
    """D2：依市場狀態調整總持股比例，其餘保留現金。

    空頭 → arg（預設 0.5）；高波動且指數在 200 日均線下 → arg；其餘（多頭、盤整、高波動上漲）→ 1。
    """
    low = float(arg) if arg else 0.5

    def wrapped(ctx: Context) -> dict[str, float]:
        return _scale(strategy(ctx), low if _bad_regime(f, ctx.date) else 1.0)
    return wrapped


def vol_target(strategy: Strategy, arg: str, f: Features) -> Strategy:
    """D1：以目標權重計算近 60 日組合波動度，總曝險＝min(1, 目標波動 ÷ 組合波動)。

    arg 為目標年化波動（%），預設 20。
    """
    target = (float(arg) if arg else 20.0) / 100

    def wrapped(ctx: Context) -> dict[str, float]:
        w = strategy(ctx)
        if not w:
            return w
        rets = f.daily_ret[list(w)].loc[:ctx.date].iloc[-60:]
        port = (rets.fillna(0) * pd.Series(w)).sum(axis=1)
        vol = float(port.std() * (245 ** 0.5))
        return _scale(w, min(1.0, target / vol) if vol > 0 else 1.0)
    return wrapped


def inverse_vol(strategy: Strategy, arg: str, f: Features) -> Strategy:
    """D3：保持總權重，個股改依 60 日波動度反比配置。"""
    def wrapped(ctx: Context) -> dict[str, float]:
        w = strategy(ctx)
        if not w:
            return w
        vol = f.at("vol_60", ctx.date).reindex(list(w)).astype(float)  # float32 加總會略超過 1
        inv = (1 / vol).where(vol > 0)
        inv = inv.fillna(inv.mean() if inv.notna().any() else 1.0)
        total = sum(w.values())
        return {c: float(v / inv.sum() * total) for c, v in inv.items()}
    return wrapped


# ── 核心衛星 ──────────────────────────────────────────────────────────────

def _bad_regime(f: Features, date: pd.Timestamp) -> bool:
    """空頭，或高波動且指數在 200 日均線下（D2 與核心衛星共用）。"""
    reg = _regime(f).loc[date]
    return bool(reg["regime"] == "空頭" or (reg["regime"] == "高波動" and reg["ma_gap"] < 0))


class CoreSatellite:
    """核心衛星配置：core_weight 放在核心標的（預設 0050），其餘依衛星策略配置。

    core_regime 不為 None 時，核心在空頭或下跌中的高波動期改持 core_weight × core_regime（其餘現金），
    與 D2 規則相同。lab 依 `core`、`core_weight`、`core_regime` 屬性做隨機選股對照與換股比例。
    """

    def __init__(self, satellite: Strategy, core_weight: float, f: Features, core: str = "0050",
                 core_regime: float | None = None):
        if not 0 <= core_weight <= 1:
            raise ValueError(f"核心比例須在 0～1，收到 {core_weight}")
        self.satellite, self.core_weight, self.core = satellite, core_weight, core
        self.f, self.core_regime = f, core_regime

    def __call__(self, ctx: Context) -> dict[str, float]:
        w = _scale(self.satellite(ctx), 1 - self.core_weight)
        core = self.core_weight
        if self.core_regime is not None and _bad_regime(self.f, ctx.date):
            core *= self.core_regime
        if core > 0:
            w[self.core] = w.get(self.core, 0.0) + core
        return w

    def with_satellite(self, satellite: Strategy) -> CoreSatellite:
        return CoreSatellite(satellite, self.core_weight, self.f, self.core, self.core_regime)


def core_satellite(arg: str, satellite: Strategy, f: Features) -> CoreSatellite:
    """arg：'70'（核心 70%）或 '70:D2'、'70:D2:0.5'（核心套市場狀態調整，壞狀態保留比例預設 0.5）。"""
    parts = arg.split(":")
    weight = float(parts[0]) / 100
    regime = None
    if len(parts) > 1:
        if parts[1] != "D2":
            raise ValueError(f"核心只支援 D2 調整，收到 {parts[1]}")
        regime = float(parts[2]) if len(parts) > 2 else 0.5
    return CoreSatellite(satellite, weight, f, core_regime=regime)


# ── 出場規則 ──────────────────────────────────────────────────────────────

class ExitRules:
    """X：停損、移動停損、市場轉壞時減碼。每天收盤檢查，隔天開盤執行（docs/研究報告/出場規則研究.md）。

    - stop：收盤價比買進價低 stop 以上就全部賣出。
    - trail：收盤價比買進後的最高收盤價低 trail 以上就全部賣出（移動停損，鎖住部分獲利）。
    - regime：市場狀態不好（空頭，或高波動且指數在 200 日均線下）時，總持股只留這個比例；
      狀態好壞轉換的當天立刻重新選股並調整，不等到月底。

    基礎策略只在每月最後一個交易日（與狀態轉換日）選股；其他日子維持現有持股，只處理出場。
    被停損賣掉的股票，到下次選股前不會再買回。價格都用還原價（除權息不會誤觸停損）。
    """

    freq = "D"
    min_trade = 0.02  # 權重差距小於總資產 2% 不交易，避免每天為了漂移來回買賣

    def __init__(self, base: Strategy, f: Features, stop: float | None = None, trail: float | None = None,
                 regime: float | None = None):
        self.base, self.f = base, f
        self.stop, self.trail, self.regime = stop, trail, regime
        self.month_ends = rebalance_dates(f.days, "M")
        self.entry: dict[str, float] = {}
        self.peak: dict[str, float] = {}
        self.bad: bool | None = None
        self.exits: list[tuple[pd.Timestamp, str, str]] = []  # (日期, 代號, 原因)

    def _price(self, field: str, i: int, code: str) -> float:
        panel = self.f.data[field]
        return float(panel.iat[i, panel.columns.get_loc(code)]) if code in panel.columns else math.nan

    def __call__(self, ctx: Context) -> dict[str, float]:
        i, held = ctx._i, ctx.weights
        for c in [c for c in self.entry if c not in held]:
            self.entry.pop(c)
            self.peak.pop(c, None)
        for c in held:
            close = self._price("adj_close", i, c)
            if c not in self.entry:  # 第一次看到持有＝今天開盤買進
                ref = self._price("adj_open", i, c)
                self.entry[c] = self.peak[c] = close if math.isnan(ref) else ref
            if not math.isnan(close):
                self.peak[c] = max(self.peak[c], close)

        bad = self.regime is not None and _bad_regime(self.f, ctx.date)
        reselect = self.bad is None or ctx.date in self.month_ends or bad != self.bad
        self.bad = bad
        if reselect:
            k = self.regime if bad else 1.0
            w = {c: v * k for c, v in self.base(ctx).items()}
        else:
            w = dict(held)

        for c in list(w):
            if c not in self.entry:
                continue
            close = self._price("adj_close", i, c)
            if math.isnan(close):
                continue
            reason = None
            if self.stop is not None and close <= self.entry[c] * (1 - self.stop):
                reason = "停損"
            elif self.trail is not None and close <= self.peak[c] * (1 - self.trail):
                reason = "移動停損"
            if reason:
                w[c] = 0.0
                self.exits.append((ctx.date, c, reason))
        return w


def _opt(x: str) -> float | None:
    return None if x in ("", "-", "0") else float(x)


def exit_rules(strategy: Strategy, arg: str, f: Features) -> ExitRules:
    """X:停損%:移動停損%:壞狀態持股比例，例如 X:15:20:0.5；不用的項目寫 -，例如 X:-:20、X:-:-:0.3。"""
    parts = (arg.split(":") + ["", "", ""])[:3]
    stop, trail = (_opt(p) for p in parts[:2])
    regime = None if parts[2] in ("", "-") else float(parts[2])
    pct = [None if x is None else x / 100 for x in (stop, trail)]
    return ExitRules(strategy, f, pct[0], pct[1], regime)


OVERLAYS = {"D1": vol_target, "D2": regime_exposure, "D3": inverse_vol, "X": exit_rules}
