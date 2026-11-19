"""市場狀態（系統規格書 STEP 03；docs/研究報告/策略研究計畫.md §3）。

每天收盤後以簡單規則分類，只用當天（含）以前的資料：

- 趨勢：加權指數相對 200 日均線，以及 200 日均線 20 日斜率。
- 波動：加權指數 20 日年化波動度，在過去 3 年（735 個交易日）中的百分位。
- 廣度：普通股收盤價高於 60 日均線的比例。

狀態：高波動（波動百分位 > 0.8，優先）、多頭（指數在均線上且均線上揚）、
空頭（指數在均線下且均線下彎）、盤整（其餘）、資料不足（指標尚未算出）。
市場狀態不是買賣訊號，用於切分績效與調整曝險。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from apex.backtest.data import MarketData

REGIMES = ["多頭", "空頭", "盤整", "高波動", "資料不足"]
HIGH_VOL_PCT = 0.8


def index_series(data: MarketData, code: str = "TAIEX") -> pd.Series:
    df = data.db.con.execute("SELECT date, close FROM index_daily WHERE index_code = ? ORDER BY date",
                             [code]).df()
    s = pd.Series(df["close"].to_numpy(), index=pd.to_datetime(df["date"]))
    return s.reindex(data.days)


def classify(index_close: pd.Series, breadth: pd.Series | None = None, ma: int = 200, slope: int = 20,
             vol_n: int = 20, vol_window: int = 735) -> pd.DataFrame:
    """指數收盤（交易日索引）→ 每日指標與狀態。純函式，方便測試。"""
    c = index_close.astype(float)
    m = c.rolling(ma, min_periods=ma).mean()
    m_slope = m / m.shift(slope) - 1
    vol = np.log(c).diff().rolling(vol_n, min_periods=vol_n).std() * np.sqrt(245)
    vol_pct = vol.rolling(vol_window, min_periods=245).rank(pct=True)
    above = c > m
    regime = pd.Series("盤整", index=c.index, dtype=object)
    regime[above & (m_slope > 0)] = "多頭"
    regime[~above & (m_slope < 0)] = "空頭"
    regime[vol_pct > HIGH_VOL_PCT] = "高波動"
    regime[m_slope.isna() | vol_pct.isna()] = "資料不足"
    out = pd.DataFrame({"close": c, "ma_gap": c / m - 1, "ma_slope": m_slope, "vol": vol, "vol_pct": vol_pct,
                        "regime": regime})
    if breadth is not None:
        out["breadth"] = breadth
    return out


def breadth(data: MarketData, n: int = 60) -> pd.Series:
    """普通股中，收盤價（還原）高於 n 日均線的比例；只計當天有成交且均線已算出的股票。"""
    sec = data.securities
    cols = data["adj_close"].columns.intersection(sec.index[sec["sec_type"] == "stock"])
    px = data["adj_close"][cols]
    ma = px.ffill().rolling(n, min_periods=n).mean()
    valid = px.notna() & ma.notna()
    return ((px > ma) & valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan)


def market_regime(data: MarketData) -> pd.DataFrame:
    return classify(index_series(data), breadth(data))


def by_regime(returns: pd.Series, regime: pd.Series) -> pd.DataFrame:
    """日報酬依當日市場狀態分組：天數、年化報酬、年化波動。

    狀態用前一天收盤的分類（交易當天開盤前已知）。
    """
    r = pd.DataFrame({"ret": returns, "regime": regime.shift(1)}).dropna()
    g = r.groupby("regime")["ret"]
    out = pd.DataFrame({"days": g.size(), "ann_return": g.mean() * 245, "ann_vol": g.std() * np.sqrt(245)})
    return out.reindex([x for x in REGIMES if x in out.index])
