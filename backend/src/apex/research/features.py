"""特徵庫（系統規格書 STEP 04；docs/研究報告/策略研究計畫.md §4）。

每個特徵是一張面板：index 為交易日、columns 為代號，第 t 列只用到 t（含）以前、且當時已公開的資料。

- 價量特徵只做往回看的滾動計算；停止買賣的日子沿用最後收盤價。
- 月營收以法定公告日（次月 10 日）的隔天、財報以 `financials_q.available_date` 起才出現在面板上，
  之後往前填補，直到下一期公告或超過 `STALE_DAYS` 個交易日。
- 計算結果快取在物件內，策略可以反覆取用；面板以 float32 儲存以節省記憶體。
"""

from __future__ import annotations

import re
from collections.abc import Callable

import numpy as np
import pandas as pd

from apex.backtest.data import MarketData
from apex.db import Database
from apex.sources.fundamentals import revenue_available

YEAR_DAYS = 245
STALE_DAYS = 260  # 基本面資料最多沿用約一年，避免已停止申報的公司一直保留舊值


def to_days(df: pd.DataFrame, days: pd.DatetimeIndex, date_col: str = "available_date",
            stale: int = STALE_DAYS) -> dict[str, pd.DataFrame]:
    """長表（code、date_col、各數值欄）→ {欄位: 交易日 × 代號面板}。

    可取得日落在非交易日時，從下一個交易日起生效；同一天有多筆時取排序在後者（較新的期別）。
    """
    if df.empty:
        return {}
    pos = days.searchsorted(pd.to_datetime(df[date_col]).values)
    df = df.assign(_pos=pos)[pos < len(days)]
    df = df.assign(_day=days[df["_pos"].to_numpy()])
    values = [c for c in df.columns if c not in ("code", date_col, "_pos", "_day")]
    out = {}
    for col in values:
        p = df.pivot_table(index="_day", columns="code", values=col, aggfunc="last")
        out[col] = p.reindex(days).ffill(limit=stale).astype("float32")
    return out


# ── 基本面：純函式，方便測試 ─────────────────────────────────────────────

def revenue_frame(rev: pd.DataFrame) -> pd.DataFrame:
    """月營收（code、year、month、revenue）→ 每期的成長特徵，附可取得日。

    - rev_yoy：單月年增率
    - rev_yoy_3m、rev_yoy_12m：近 3、12 個月合計的年增率
    - rev_accel：rev_yoy_3m − rev_yoy_12m（營收加速）
    - rev_up_streak：單月年增率連續為正的月數
    期別必須連續才計算；缺月時相關數值為 NaN。
    """
    rev = rev.dropna(subset=["revenue"])
    idx = (rev["year"] * 12 + rev["month"] - 1).astype(int)
    p = pd.pivot_table(rev.assign(p=idx), index="p", columns="code", values="revenue", aggfunc="last")
    p = p.reindex(range(p.index.min(), p.index.max() + 1)).astype(float)
    ly = p.shift(12)

    def growth(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
        return (a / b - 1).where(b > 0)

    s3, s12 = p.rolling(3).sum(), p.rolling(12).sum()
    feats = {
        "rev_yoy": growth(p, ly),
        "rev_yoy_3m": growth(s3, s3.shift(12)),
        "rev_yoy_12m": growth(s12, s12.shift(12)),
    }
    feats["rev_accel"] = feats["rev_yoy_3m"] - feats["rev_yoy_12m"]
    pos = (feats["rev_yoy"] > 0).astype(float).where(feats["rev_yoy"].notna())
    streak = pos.copy()
    for i in range(1, len(pos)):
        streak.iloc[i] = (streak.iloc[i - 1].fillna(0) + 1) * pos.iloc[i]
    feats["rev_up_streak"] = streak
    long = pd.concat({k: v.stack() for k, v in feats.items()}, axis=1).reset_index()
    long.columns = ["p", "code", *feats]
    long = long[long["rev_yoy"].notna() | long["rev_yoy_12m"].notna()]
    long["available_date"] = [revenue_available(p // 12, p % 12 + 1) for p in long["p"]]
    return long.sort_values(["available_date", "p"]).drop(columns="p")


def financial_frame(fin: pd.DataFrame) -> pd.DataFrame:
    """單季財報（financials_q）→ 每季的獲利特徵，附可取得日。

    - eps_yoy：單季 EPS 相對去年同季的變化 ÷ |去年同季 EPS|
    - gross_margin、op_margin 與年增變化 gm_chg、om_chg（百分點）
    - roe_ttm：近 4 季歸屬母公司淨利 ÷ 期末歸屬母公司權益
    - debt_ratio：負債 ÷ 資產；shares：股本 ÷ 10（估計股數，面額非 10 元者會有誤差）
    - eps_ttm：近 4 季 EPS 合計；bvps：每股淨值（沒有時用歸屬母公司權益 ÷ 估計股數）
    """
    f = fin.copy()
    f["q"] = (f["year"] * 4 + f["quarter"] - 1).astype(int)
    f = f.sort_values(["code", "q"])
    net = f["net_income_parent"].fillna(f["net_income"])
    f["gross_margin"] = (f["gross_profit"] / f["revenue"]).where(f["revenue"] > 0)
    f["op_margin"] = (f["operating_income"] / f["revenue"]).where(f["revenue"] > 0)
    f["debt_ratio"] = (f["total_liabilities"] / f["total_assets"]).where(f["total_assets"] > 0)
    f["shares"] = f["share_capital"] / 10
    f["_net"] = net
    g = f.set_index(["code", "q"])

    def lag(col: str, n: int) -> pd.Series:
        """同一公司 n 季前的數值；該季缺漏時為 NaN。"""
        key = pd.MultiIndex.from_arrays([g.index.get_level_values("code"), g.index.get_level_values("q") - n])
        return pd.Series(g[col].reindex(key).to_numpy(), index=g.index)

    eps_ly = lag("eps", 4)
    g["eps_yoy"] = ((g["eps"] - eps_ly) / eps_ly.abs()).where(eps_ly.abs() > 0)
    g["gm_chg"] = g["gross_margin"] - lag("gross_margin", 4)
    g["om_chg"] = g["op_margin"] - lag("op_margin", 4)
    ttm = g["_net"] + lag("_net", 1) + lag("_net", 2) + lag("_net", 3)
    equity = g["equity_parent"].fillna(g["equity"])
    g["roe_ttm"] = (ttm / equity).where(equity > 0)
    g["eps_ttm"] = g["eps"] + lag("eps", 1) + lag("eps", 2) + lag("eps", 3)
    bvps = g["bvps"] if "bvps" in g else pd.Series(np.nan, index=g.index)
    g["bvps"] = bvps.fillna((equity / g["shares"]).where(g["shares"] > 0))
    cols = ["available_date", "eps", "eps_yoy", "gross_margin", "op_margin", "gm_chg", "om_chg", "roe_ttm",
            "debt_ratio", "shares", "eps_ttm", "bvps"]
    out = g[cols].reset_index()
    return out.sort_values(["available_date", "q"]).drop(columns="q")


def payout_panel(actions: pd.DataFrame, days: pd.DatetimeIndex, cols: pd.Index) -> pd.DataFrame:
    """除權息（code、eff_date、prev_close、ref_price）→ 近 12 個月的配發率面板。

    每次除權息的配發率＝(除權息前收盤 − 參考價) ÷ 除權息前收盤（現金股利與股票股利都算）；
    從除權息生效日起計入，一年（YEAR_DAYS 個交易日）後移出。除權息日前就已公告，沒有偷看未來。
    """
    out = pd.DataFrame(0.0, index=days, columns=cols)
    if not actions.empty:
        a = actions[(actions["prev_close"] > 0) & actions["ref_price"].notna()]
        rate = (1 - a["ref_price"] / a["prev_close"]).clip(lower=0, upper=1)
        pos = days.searchsorted(pd.to_datetime(a["eff_date"]).values)
        ok = pos < len(days)
        ev = pd.DataFrame({"_day": days[pos[ok]], "code": a["code"].to_numpy()[ok],
                           "rate": rate.to_numpy()[ok]})
        p = ev.pivot_table(index="_day", columns="code", values="rate", aggfunc="sum")
        out = out.add(p.reindex(index=days, columns=cols).fillna(0.0), fill_value=0.0)
    return out.rolling(YEAR_DAYS, min_periods=1).sum()


# ── 特徵庫 ─────────────────────────────────────────────────────────────

class Features:
    """依名稱取得特徵面板，例如 `f["ret_60"]`、`f["mom_240_20"]`、`f["rev_accel"]`。

    名稱格式見 `PATTERNS`；`Features.names()` 列出主要特徵。
    """

    def __init__(self, data: MarketData, db: Database | None = None):
        self.data = data
        self.db = db if db is not None else data.db
        self.days = data.days
        self._cache: dict[str, pd.DataFrame] = {}
        sec = data.securities
        self.stocks = data["close"].columns.intersection(sec.index[sec["sec_type"] == "stock"])

    def __getitem__(self, name: str) -> pd.DataFrame:
        if name not in self._cache:
            for pattern, fn in self.PATTERNS:
                m = re.fullmatch(pattern, name)
                if m:
                    result = fn(self, *(int(x) for x in m.groups()))
                    if isinstance(result, dict):  # 一次算出多個特徵
                        self._cache.update(result)
                    else:
                        self._cache[name] = result.astype("float32")
                    break
            else:
                raise KeyError(f"未知的特徵：{name}")
        return self._cache[name]

    def at(self, name: str, date: pd.Timestamp) -> pd.Series:
        """date 當天的橫斷面（策略在訊號日使用）。"""
        return self[name].loc[date]

    @staticmethod
    def names() -> list[str]:
        return ["ret_{n}", "mom_{n}_{skip}", "ma_gap_{n}", "ma_slope_{n}", "high_gap_{n}", "vol_{n}",
                "downvol_{n}", "mdd_{n}", "gap_{n}", "value_{n}", "value_ratio_{a}_{b}", "rank_ret_{n}",
                "ind_ret_{n}", "ind_rel_{n}", "foreign_{n}", "trust_{n}", "dealer_{n}", "margin_chg_{n}",
                "short_ratio", "rev_yoy", "rev_yoy_3m", "rev_yoy_12m", "rev_accel", "rev_up_streak",
                "eps", "eps_yoy", "gross_margin", "op_margin", "gm_chg", "om_chg", "roe_ttm", "debt_ratio",
                "shares", "mcap", "industry_code", "ey", "bp", "dy"]

    # 價量 ---------------------------------------------------------------
    @property
    def px(self) -> pd.DataFrame:
        """還原收盤價，停止買賣的日子沿用最後價格（只往前填，不跨越上市前）。"""
        if "_px" not in self._cache:
            self._cache["_px"] = self.data["adj_close"].ffill()
        return self._cache["_px"]

    @property
    def daily_ret(self) -> pd.DataFrame:
        if "_dret" not in self._cache:
            self._cache["_dret"] = self.px / self.px.shift(1) - 1
        return self._cache["_dret"]

    def _ret(self, n: int) -> pd.DataFrame:
        return self.px / self.px.shift(n) - 1

    def _mom(self, n: int, skip: int) -> pd.DataFrame:
        return self.px.shift(skip) / self.px.shift(n) - 1

    def _ma_gap(self, n: int) -> pd.DataFrame:
        return self.px / self.px.rolling(n, min_periods=n).mean() - 1

    def _ma_slope(self, n: int) -> pd.DataFrame:
        ma = self.px.rolling(n, min_periods=n).mean()
        return ma / ma.shift(20) - 1

    def _high_gap(self, n: int) -> pd.DataFrame:
        return self.px / self.px.rolling(n, min_periods=n).max() - 1

    def _vol(self, n: int) -> pd.DataFrame:
        return self.daily_ret.rolling(n, min_periods=n).std() * np.sqrt(YEAR_DAYS)

    def _downvol(self, n: int) -> pd.DataFrame:
        neg = self.daily_ret.clip(upper=0)
        return np.sqrt((neg ** 2).rolling(n, min_periods=n).mean() * YEAR_DAYS)

    def _mdd(self, n: int) -> pd.DataFrame:
        """近 n 日內，相對 n 日高點的最大跌幅（負值）。"""
        return (self.px / self.px.rolling(n, min_periods=n).max() - 1).rolling(n, min_periods=n).min()

    def _gap(self, n: int) -> pd.DataFrame:
        """近 n 日最大的開盤跳空幅度（絕對值）。"""
        gap = (self.data["adj_open"] / self.px.shift(1) - 1).abs()
        return gap.rolling(n, min_periods=1).max()

    def _value(self, n: int) -> pd.DataFrame:
        """近 n 日平均成交金額（未成交日以 0 計）。"""
        return self.data["value"].fillna(0).rolling(n, min_periods=n).mean()

    def _value_ratio(self, a: int, b: int) -> pd.DataFrame:
        return (self._value(a) / self._value(b)).where(self._value(b) > 0)

    def _rank_ret(self, n: int) -> pd.DataFrame:
        """普通股 n 日報酬的橫斷面百分位（0～1，1 最強）。"""
        r = self[f"ret_{n}"][self.stocks]
        return r.rank(axis=1, pct=True).reindex(columns=self.data["close"].columns)

    # 產業 ---------------------------------------------------------------
    def industry(self) -> pd.DataFrame:
        """時點產業別（交易日 × 代號，字串）：取當時最新一期月營收上的產業別，沒有時用證券主檔。"""
        if "_industry" not in self._cache:
            rev = self.db.con.execute("""
                SELECT code, year, month, industry FROM monthly_revenue WHERE industry IS NOT NULL
            """).df()
            periods = zip(rev["year"], rev["month"], strict=True)
            rev["available_date"] = [revenue_available(y, m) for y, m in periods]
            rev = rev.sort_values(["available_date", "year", "month"])
            pos = self.days.searchsorted(pd.to_datetime(rev["available_date"]).values)
            rev = rev[pos < len(self.days)].assign(_day=self.days[pos[pos < len(self.days)]])
            p = rev.pivot_table(index="_day", columns="code", values="industry", aggfunc="last")
            p = p.reindex(index=self.days, columns=self.data["close"].columns).ffill()
            fallback = self.data.securities["industry"].reindex(p.columns)
            self._cache["_industry"] = p.fillna(fallback)
        return self._cache["_industry"]

    def _industry_code(self) -> pd.DataFrame:
        ind = self.industry()
        cats = pd.Categorical(ind.to_numpy().ravel())
        codes = cats.codes.reshape(ind.shape).astype("float32")
        codes[codes < 0] = np.nan
        self.industry_names = list(cats.categories)
        return pd.DataFrame(codes, index=ind.index, columns=ind.columns)

    def _ind_ret(self, n: int) -> pd.DataFrame:
        """產業 n 日報酬：同產業普通股的成交金額加權平均（權重為近 20 日平均成交金額）。"""
        r = self[f"ret_{n}"][self.stocks]
        w = self["value_20"][self.stocks].where(r.notna())
        ind = self["industry_code"][self.stocks]
        rv, wv, iv = r.to_numpy(), w.fillna(0).to_numpy(), ind.to_numpy()
        res = np.full(rv.shape, np.nan, dtype="float32")
        for k in np.unique(iv[~np.isnan(iv)]):
            mask = iv == k
            num = np.where(mask, np.nan_to_num(rv) * wv, 0).sum(axis=1)
            den = np.where(mask & ~np.isnan(rv), wv, 0).sum(axis=1)
            avg = np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)
            res[mask] = np.broadcast_to(avg[:, None], mask.shape)[mask]
        return pd.DataFrame(res, index=r.index, columns=r.columns).reindex(columns=self.data["close"].columns)

    def _ind_rel(self, n: int) -> pd.DataFrame:
        return self[f"ret_{n}"] - self[f"ind_ret_{n}"]

    # 籌碼 ---------------------------------------------------------------
    def _chips(self) -> dict[str, pd.DataFrame]:
        cols = self.data["close"].columns
        first, last = self.days[0].date(), self.days[-1].date()
        inst = self.db.con.execute("""
            SELECT date, code, foreign_net::DOUBLE AS f, trust_net::DOUBLE AS t, dealer_net::DOUBLE AS d
            FROM institutional WHERE date BETWEEN ? AND ?
        """, [first, last]).df()
        marg = self.db.con.execute("""
            SELECT date, code, margin_balance::DOUBLE AS mb, short_balance::DOUBLE AS sb
            FROM margin WHERE date BETWEEN ? AND ?
        """, [first, last]).df()
        out = {}
        for df in (inst, marg):
            df["date"] = pd.to_datetime(df["date"])
        traded = self.data["volume"].notna()
        for key, col in (("_foreign", "f"), ("_trust", "t"), ("_dealer", "d")):
            p = inst.pivot(index="date", columns="code", values=col).reindex(index=self.days, columns=cols)
            out[key] = p.fillna(0).where(traded)  # 有成交但未列出＝法人未交易
        for key, col in (("_margin_bal", "mb"), ("_short_bal", "sb")):
            # 轉上市當天兩個市場可能各有一筆（2 例），取較大者（新市場的餘額）
            p = marg.pivot_table(index="date", columns="code", values=col, aggfunc="max")
            out[key] = p.reindex(index=self.days, columns=cols)
        return out

    def _inst(self, key: str, n: int) -> pd.DataFrame:
        if key not in self._cache:
            self._cache.update(self._chips())
        net = self._cache[key].fillna(0).rolling(n, min_periods=n).sum()
        vol = self.data["volume"].fillna(0).rolling(n, min_periods=n).sum()
        return (net / vol).where(vol > 0)

    def _margin_chg(self, n: int) -> pd.DataFrame:
        if "_margin_bal" not in self._cache:
            self._cache.update(self._chips())
        mb = self._cache["_margin_bal"].ffill(limit=5)
        return (mb / mb.shift(n) - 1).where(mb.shift(n) > 0)

    def _short_ratio(self) -> pd.DataFrame:
        if "_margin_bal" not in self._cache:
            self._cache.update(self._chips())
        mb, sb = self._cache["_margin_bal"], self._cache["_short_bal"]
        return (sb / mb).where(mb > 0)

    # 基本面 -------------------------------------------------------------
    def _revenue(self) -> dict[str, pd.DataFrame]:
        rev = self.db.con.execute(
            "SELECT code, year, month, revenue::DOUBLE AS revenue FROM monthly_revenue").df()
        frame = revenue_frame(rev)
        cols = self.data["close"].columns
        return {k: v.reindex(columns=cols) for k, v in to_days(frame, self.days).items()}

    def _financials(self) -> dict[str, pd.DataFrame]:
        fin = self.db.con.execute("SELECT * FROM financials_q").df()
        frame = financial_frame(fin)
        cols = self.data["close"].columns
        return {k: v.reindex(columns=cols) for k, v in to_days(frame, self.days).items()}

    @property
    def raw_close(self) -> pd.DataFrame:
        """未還原收盤價（停止買賣沿用最後價格）：和每股盈餘、淨值比較時用。"""
        if "_rclose" not in self._cache:
            self._cache["_rclose"] = self.data["close"].ffill()
        return self._cache["_rclose"]

    def _ey(self) -> pd.DataFrame:
        """盈餘殖利率：近 4 季 EPS ÷ 股價（本益比的倒數；虧損為負值，仍可排序）。"""
        px = self.raw_close
        return (self["eps_ttm"] / px).where(px > 0)

    def _bp(self) -> pd.DataFrame:
        """淨值股價比：每股淨值 ÷ 股價（股價淨值比的倒數）。"""
        px = self.raw_close
        return (self["bvps"] / px).where(px > 0)

    def _dy(self) -> pd.DataFrame:
        """近 12 個月配發率（現金加股票股利，以除權息前股價計）；上市未滿一年或沒有配發為 0。"""
        actions = self.db.con.execute("""
            SELECT code, eff_date, prev_close, ref_price FROM corporate_actions_eff
            WHERE kind = 'dividend' AND eff_date IS NOT NULL
        """).df()
        cols = self.data["close"].columns
        return payout_panel(actions, self.days, cols).where(self.data["close"].ffill().notna())

    def _mcap(self) -> pd.DataFrame:
        """估計市值：股數（股本 ÷ 10）× 未還原收盤價。"""
        return self["shares"] * self.data["close"].ffill()

    PATTERNS: list[tuple[str, Callable]] = [
        (r"ret_(\d+)", _ret),
        (r"mom_(\d+)_(\d+)", _mom),
        (r"ma_gap_(\d+)", _ma_gap),
        (r"ma_slope_(\d+)", _ma_slope),
        (r"high_gap_(\d+)", _high_gap),
        (r"vol_(\d+)", _vol),
        (r"downvol_(\d+)", _downvol),
        (r"mdd_(\d+)", _mdd),
        (r"gap_(\d+)", _gap),
        (r"value_(\d+)", _value),
        (r"value_ratio_(\d+)_(\d+)", _value_ratio),
        (r"rank_ret_(\d+)", _rank_ret),
        (r"industry_code", _industry_code),
        (r"ind_ret_(\d+)", _ind_ret),
        (r"ind_rel_(\d+)", _ind_rel),
        (r"foreign_(\d+)", lambda self, n: self._inst("_foreign", n)),
        (r"trust_(\d+)", lambda self, n: self._inst("_trust", n)),
        (r"dealer_(\d+)", lambda self, n: self._inst("_dealer", n)),
        (r"margin_chg_(\d+)", _margin_chg),
        (r"short_ratio", _short_ratio),
        (r"rev_yoy|rev_yoy_3m|rev_yoy_12m|rev_accel|rev_up_streak", _revenue),
        (r"eps|eps_yoy|gross_margin|op_margin|gm_chg|om_chg|roe_ttm|debt_ratio|shares|eps_ttm|bvps",
         _financials),
        (r"ey", _ey),
        (r"bp", _bp),
        (r"dy", _dy),
        (r"mcap", _mcap),
    ]
