"""候選股票（系統規格書 STEP 05B、STEP 08 第 3、4 項；docs/候選股票.md）。

每天用最新資料：
1. 以「到今天為止已實現的 60 日／20 日標籤」訓練 LightGBM，預測今天每檔可投資股票的分數。
2. 分數轉成當日百分位，依等級查「歷史樣本外戰績表」（2018–2024 滾動式預測的真實結果），
   得到該等級的上漲機率、報酬中間值、平均值與 25～75% 區間，再換算成價位。
3. 取 60 日分數前 N 名當候選，附上 20 日的等級與戰績當參考。

重要：這些模型在研究中**沒有通過驗證期**（docs/研究報告/機器學習研究報告.md）。預期區間是歷史統計，不是保證；
要不要買由使用者決定（產品企劃書：系統提供分析，不自動下單）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

from apex.backtest.data import MarketData
from apex.research.features import Features
from apex.research.ml import ML_START, build_dataset, make_model, walk_forward

MODEL = "lgbm"
HORIZONS = (60, 20)  # 60 日為主要決策週期（使用者 2026-11-19 決定），20 日為輔助
CALIBRATION_YEARS = list(range(2018, 2025))  # 研究期＋驗證期的樣本外預測；不使用保留期
BUCKETS = [(0.98, 1.0, "前 2%"), (0.95, 0.98, "前 2～5%"), (0.90, 0.95, "前 5～10%"),
           (0.80, 0.90, "前 10～20%"), (0.50, 0.80, "前 20～50%"), (0.0, 0.50, "後 50%")]
YEAR_DAYS = 245
QUANTILES = {"q10": .10, "q25": .25, "q50": .50, "q75": .75, "q90": .90}
ROUND_TRIP_COST = 0.0078  # 手續費 0.1425%×2＋證交稅 0.3%＋滑價 0.1%×2（約略）


def bucket_of(pct: float) -> str:
    for lo, hi, name in BUCKETS:
        if lo < pct <= hi or (lo == 0 and pct <= hi):
            return name
    return BUCKETS[-1][2]


def calibration(pred: pd.Series, y: pd.DataFrame, vol: pd.Series | None = None, horizon: int = 60
                ) -> pd.DataFrame:
    """依分數等級統計實際的持有期間報酬：上漲機率、10/25/50/75/90 百分位、平均、樣本數。

    給 vol（訊號日的年化波動）時，另外統計「報酬 ÷ 期間波動」的百分位（z_q10…z_mean），
    讓每檔股票可以依自己的波動換算出專屬的區間：波動大的股票，區間自然比較寬。
    """
    df = pd.DataFrame({"score": pred}).join(y[["fwd_ret"]]).dropna()
    if vol is not None:
        df["z"] = df["fwd_ret"] / (vol.reindex(df.index) * np.sqrt(horizon / YEAR_DAYS))
    df["pct"] = df.groupby(level="date")["score"].rank(pct=True)
    df["bucket"] = df["pct"].map(bucket_of)

    def stats(d: pd.DataFrame) -> dict:
        r = d["fwd_ret"]
        out = {"up": float((r > 0).mean()), **{k: float(r.quantile(q)) for k, q in QUANTILES.items()},
               "mean": float(r.mean()), "n": len(d)}
        if "z" in d:
            z = d["z"].replace([np.inf, -np.inf], np.nan).dropna()
            out |= {f"z_{k}": float(z.quantile(q)) for k, q in QUANTILES.items()}
            out["z_mean"] = float(z.clip(z.quantile(.01), z.quantile(.99)).mean())
        return out

    rows = {b: stats(g) for b, g in df.groupby("bucket")}
    rows["全部"] = stats(df)
    return pd.DataFrame(rows).T.reindex([b[2] for b in BUCKETS] + ["全部"])


@dataclass
class HorizonResult:
    horizon: int
    scores: pd.Series  # 今天的分數（代號 → 百分位 0～1）
    table: pd.DataFrame  # 戰績表（等級 → 統計）
    trained_rows: int
    train_label_end: pd.Timestamp
    contrib: pd.DataFrame | None = None  # 今天每檔股票各特徵對分數的貢獻（代號 × 特徵）


# ── 推薦理由：把模型的特徵貢獻翻成白話 ────────────────────────────────────

def _pct(x: float) -> str:
    return f"{x:+.0%}" if abs(x) >= 0.1 else f"{x:+.1%}"


def _pp(x: float) -> str:
    return f"{x * 100:+.1f} 個百分點"


def _yi(x: float) -> str:
    return f"{x / 1e8:,.1f} 億元" if x < 1e12 else f"{x / 1e12:,.1f} 兆元"


# 特徵 → (白話名稱, 數值格式)
LABELS: dict[str, tuple[str, object]] = {
    "ret_5": ("近 1 週漲跌", _pct), "ret_20": ("近 1 個月漲跌", _pct), "ret_60": ("近 3 個月漲跌", _pct),
    "ret_120": ("近半年漲跌", _pct), "mom_240_20": ("近一年漲跌（不含最近 1 個月）", _pct),
    "ma_gap_20": ("股價距月線", _pct), "ma_gap_60": ("股價距季線", _pct), "ma_gap_240": ("股價距年線", _pct),
    "ma_slope_60": ("季線近 1 個月走勢", _pct), "ma_slope_120": ("半年線近 1 個月走勢", _pct),
    "high_gap_240": ("距一年最高點", _pct), "vol_20": ("近 1 個月波動", lambda x: f"{x:.0%}"),
    "vol_60": ("近 3 個月波動", lambda x: f"{x:.0%}"), "downvol_60": ("近 3 個月下跌波動", lambda x: f"{x:.0%}"),
    "mdd_120": ("近半年最大跌幅", _pct), "gap_20": ("近 1 個月最大跳空", lambda x: f"{x:.1%}"),
    "value_20": ("近 1 個月日均成交金額", _yi),
    "value_ratio_5_60": ("近 1 週成交量是 3 個月平均的", lambda x: f"{x:.1f} 倍"),
    "ind_ret_60": ("所屬產業近 3 個月漲跌", _pct), "ind_rel_60": ("近 3 個月比同產業強／弱", _pct),
    "ind_rel_120": ("近半年比同產業強／弱", _pct),
    "foreign_20": ("外資近 1 個月買賣超（占成交量）", _pct), "foreign_60": ("外資近 3 個月買賣超（占成交量）", _pct),
    "trust_20": ("投信近 1 個月買賣超（占成交量）", _pct), "trust_60": ("投信近 3 個月買賣超（占成交量）", _pct),
    "dealer_20": ("自營商近 1 個月買賣超（占成交量）", _pct), "margin_chg_20": ("融資餘額近 1 個月變化", _pct),
    "short_ratio": ("券資比", lambda x: f"{x:.1%}"),
    "rev_yoy": ("最新月營收年增", _pct), "rev_yoy_3m": ("近 3 個月營收年增", _pct),
    "rev_yoy_12m": ("近 12 個月營收年增", _pct), "rev_accel": ("營收成長加速（近 3 月比近 12 月）", _pp),
    "rev_up_streak": ("營收連續年增", lambda x: f"{x:.0f} 個月"),
    "eps": ("最近一季 EPS", lambda x: f"{x:.2f} 元"), "eps_yoy": ("最近一季 EPS 年增", _pct),
    "gross_margin": ("毛利率", lambda x: f"{x:.1%}"), "op_margin": ("營業利益率", lambda x: f"{x:.1%}"),
    "gm_chg": ("毛利率比去年同季", _pp), "om_chg": ("營業利益率比去年同季", _pp),
    "roe_ttm": ("近四季 ROE", lambda x: f"{x:.1%}"), "debt_ratio": ("負債比", lambda x: f"{x:.0%}"),
    "mcap": ("市值", _yi),
}


def reasons(contrib: pd.Series, raw: dict[str, float], n_pos: int = 3, n_neg: int = 2) -> dict:
    """依特徵貢獻挑出最支持（pros）與最扣分（cons）的幾項，附上實際數值。市場特徵大家都一樣，不列入。"""
    c = contrib[[k for k in contrib.index if k in LABELS]].dropna()

    def line(k: str) -> str:
        label, fmt = LABELS[k]
        v = raw.get(k)
        return f"{label} {fmt(v)}" if v is not None and not pd.isna(v) else f"{label}（無資料）"

    pros = [line(k) for k, v in c.sort_values(ascending=False).items() if v > 0][:n_pos]
    cons = [line(k) for k, v in c.sort_values().items() if v < 0][:n_neg]
    return {"pros": pros, "cons": cons}


def checks(raw: dict[str, float]) -> list[dict]:
    """量化策略的條件（Phase 2 規則）是否成立，用來看「規則」和「AI」意見一致或衝突。"""
    def ok(*conds) -> bool | None:
        if any(v is None or pd.isna(v) for v, _ in conds):
            return None
        return all(fn(v) for v, fn in conds)

    g = raw.get

    def pos(v: float) -> bool:
        return v > 0
    return [
        {"name": "趨勢", "desc": "股價在月、季、年線之上，且季線、半年線往上",
         "ok": ok((g("ma_gap_20"), pos), (g("ma_gap_60"), pos), (g("ma_gap_240"), pos),
                  (g("ma_slope_60"), pos), (g("ma_slope_120"), pos))},
        {"name": "營收成長", "desc": "近 3 個月營收年增，且成長在加速",
         "ok": ok((g("rev_yoy_3m"), pos), (g("rev_accel"), pos))},
        {"name": "獲利改善", "desc": "最近一季 EPS 年增，且毛利率比去年高",
         "ok": ok((g("eps_yoy"), pos), (g("gm_chg"), pos))},
        {"name": "法人買進", "desc": "外資或投信近 1 個月買超",
         "ok": None if g("foreign_20") is None or g("trust_20") is None
         else bool((g("foreign_20") or 0) > 0 or (g("trust_20") or 0) > 0)},
    ]


def advice(pct60: float, raw: dict[str, float]) -> dict:
    """持股看法（規則公開、固定）：模型分數後 20% 且跌破季線 → 考慮減碼；分數後半或跌破季線 → 留意；其餘續抱。"""
    below = raw.get("ma_gap_60") is not None and not pd.isna(raw.get("ma_gap_60")) and raw["ma_gap_60"] < 0
    if pct60 < 0.2 and below:
        return {"level": "考慮減碼", "why": "模型分數在後 20%，而且股價跌破季線"}
    if pct60 < 0.5 or below:
        why = []
        if pct60 < 0.5:
            why.append("模型分數在後半段")
        if below:
            why.append("股價在季線之下")
        return {"level": "留意", "why": "、".join(why)}
    return {"level": "續抱", "why": "模型分數在前半段，且股價在季線之上"}


def predict_today(f: Features, data_date: pd.Timestamp, horizon: int, model: str = MODEL,
                  calibration_end: dt.date = dt.date(2024, 12, 31)) -> HorizonResult:
    """以到 data_date 已實現的標籤訓練，預測 data_date 當天的分數，並附上歷史戰績表。"""
    days = f.data.days
    month_ends = sorted({d for d in pd.Series(days, index=days).groupby(days.to_period("M")).max()
                         if days.get_loc(d) >= 240 and d < data_date})
    ds = build_dataset(f, horizon, dates=month_ends + [data_date])
    dates = ds.X.index.get_level_values("date")

    # 歷史戰績：只用 calibration_end 以前的樣本外預測（研究期＋驗證期），保留期不看
    cal_mask = dates <= pd.Timestamp(calibration_end)
    cal_ds = type(ds)(ds.X[cal_mask], ds.y[cal_mask], horizon)
    pred, _ = walk_forward(cal_ds, model, CALIBRATION_YEARS)
    vol = f["vol_60"].stack()
    vol.index.names = ["date", "code"]
    table = calibration(pred, cal_ds.y, vol, horizon)

    # 今天的模型：用標籤在 data_date 以前已實現的全部樣本
    train = (ds.y["label_end"] <= data_date).to_numpy() & ds.y["label"].notna().to_numpy()
    m = make_model(model)
    m.fit(ds.X[train], ds.y["label"][train].to_numpy())
    today = dates == data_date
    Xt = ds.X[today]
    codes = ds.X.index[today].get_level_values("code")
    raw = pd.Series(m.predict(Xt), index=codes)
    contrib = None
    if model == "lgbm":  # LightGBM 內建 SHAP 貢獻：每個特徵把分數往上或往下推多少
        contrib = pd.DataFrame(m.predict(Xt, pred_contrib=True)[:, :-1], index=codes, columns=Xt.columns)
    return HorizonResult(horizon, raw.rank(pct=True), table, int(train.sum()), ds.y["label_end"][train].max(),
                         contrib)


def candidates(f: Features, data_date: pd.Timestamp, n: int = 30) -> dict:
    """今天的候選股票（60 日分數前 n 名），附預期報酬區間、價位與風險。

    網站依使用者輸入的投入金額，從這 n 檔中組出建議組合（分散產業、依波動配置、買不起就換下一名），
    所以名單比實際建議的檔數多。
    """
    res = {h: predict_today(f, data_date, h) for h in HORIZONS}
    main = res[HORIZONS[0]]
    sec = f.data.securities
    close = f.data["close"].loc[data_date]
    vol = f["vol_60"].loc[data_date]
    mdd = f["mdd_120"].loc[data_date]
    ind = f.industry().loc[data_date]
    rows = []
    raw_all = {k: f[k].loc[data_date] for k in LABELS}

    def raw_of(code: str) -> dict[str, float]:
        return {k: _f(v.get(code)) for k, v in raw_all.items()}

    for code in main.scores.sort_values(ascending=False).head(n).index:
        c = float(close.get(code, np.nan))
        row = {"code": code, "rank": len(rows) + 1, "pct": float(main.scores[code]),
               "name": str(sec.at[code, "name"]) if code in sec.index else "",
               "industry": None if pd.isna(ind.get(code)) else str(ind.get(code)),
               "close": c, "vol_60": _f(vol.get(code)), "mdd_120": _f(mdd.get(code))}
        rv = raw_of(code)
        if main.contrib is not None:
            row["reasons"] = reasons(main.contrib.loc[code], rv)
        row["checks"] = checks(rv)
        for h, r in res.items():
            p = float(r.scores.get(code, np.nan))
            b = bucket_of(p) if not np.isnan(p) else None
            t = r.table.loc[b] if b else None
            if t is None:
                row[f"h{h}"] = None
                continue
            # 中間值與平均用該等級的歷史實際值（波動大不代表比較會漲）；
            # 區間寬度依這檔股票自己的波動調整（波動大的股票漲跌範圍較寬）。沒有波動資料時用等級整體統計。
            sd = row["vol_60"] * np.sqrt(h / YEAR_DAYS) if row["vol_60"] else None
            mid, z50 = float(t["q50"]), float(t["z_q50"])
            q = {k: mid + (float(t[f"z_{k}"]) - z50) * float(sd) if sd else float(t[k]) for k in QUANTILES}
            q["mean"] = float(t["mean"])
            row[f"h{h}"] = {"pct": p, "bucket": b, "up": float(t["up"]), **q,
                            "price": {k: round(float(c * (1 + q[k])), 2) for k in ("q25", "q50", "q75")},
                            "net_mean": q["mean"] - ROUND_TRIP_COST, "net_q50": q["q50"] - ROUND_TRIP_COST}
        rows.append(row)
    base = {h: {k: float(r.table.loc["全部", k]) for k in ("up", "q25", "q50", "q75", "mean")}
            for h, r in res.items()}
    # 候選股之間近 60 日日報酬的相關係數（建議組合避開走勢太像的股票）
    codes = [r["code"] for r in rows]
    corr = f.daily_ret[codes].loc[:data_date].iloc[-60:].corr().round(2)
    correlation = {"codes": codes, "matrix": [[None if pd.isna(v) else float(v) for v in row] for row in corr.values]}

    # 全部可投資股票的評分與持股看法（網站「我的持股」用；只放進加密檔）
    scores = {}
    for code, p in main.scores.items():
        rv = raw_of(code)
        b = bucket_of(float(p))
        t = main.table.loc[b]
        p20 = res[HORIZONS[1]].scores.get(code)
        scores[code] = {"pct": round(float(p), 4), "pct20": None if p20 is None else round(float(p20), 4),
                        "bucket": b, "up": round(float(t["up"]), 4), "q50": round(float(t["q50"]), 4),
                        "advice": advice(float(p), rv), "checks": checks(rv),
                        **({"reasons": reasons(main.contrib.loc[code], rv)} if main.contrib is not None else {})}
    cols = ("up", "q25", "q50", "q75", "mean", "n")
    tables = {h: [{"bucket": b, **{k: float(r.table.loc[b, k]) for k in cols}} for b in r.table.index]
              for h, r in res.items()}
    return {"data_date": str(pd.Timestamp(data_date).date()), "model": MODEL, "horizons": list(HORIZONS),
            "cost": ROUND_TRIP_COST, "candidates": rows, "scores": scores, "base": base, "tables": tables,
            "correlation": correlation,
            "trained_until": str(main.train_label_end.date()), "calibration": "2018–2024 樣本外"}


def _f(x) -> float | None:
    return None if x is None or pd.isna(x) else float(x)


def load_for(db, data_date: dt.date) -> Features:
    from apex.backtest import load
    data: MarketData = load(db, ML_START, data_date)
    return Features(data)


def text(c: dict) -> str:
    """本機每日報告用的文字版。"""
    h = c["horizons"][0]
    base = c["base"][h]
    lines = [f"候選股票｜資料日 {c['data_date']}｜模型 LightGBM（每日用已實現資料重新訓練）",
             "",
             f"**注意：此模型在歷史驗證中沒有通過門檻；以下是歷史統計，不是保證。** "
             f"全部股票 {h} 日後上漲機率 {base['up']:.0%}、中間值 {base['q50']:+.1%}。",
             "",
             f"| 股票 | 收盤 | 等級 | {h}日上漲機率 | 中間值（價位） | 一半落在 | 平均（扣成本） |",
             "|---|---|---|---|---|---|---|"]
    for r in c["candidates"][:10]:
        x = r[f"h{h}"]
        if not x:
            continue
        lines.append(f"| {r['code']} {r['name']} | {r['close']:,.2f} | {x['bucket']} | {x['up']:.0%} | "
                     f"{x['q50']:+.1%}（{x['price']['q50']:,.2f}） | {x['q25']:+.1%}～{x['q75']:+.1%}"
                     f"（{x['price']['q25']:,.2f}～{x['price']['q75']:,.2f}） | {x['net_mean']:+.1%} |")
    lines += ["", "推薦理由（模型最看重的幾項）："]
    for r in c["candidates"][:10]:
        rs = r.get("reasons") or {}
        ok = "、".join(k["name"] for k in r.get("checks", []) if k["ok"]) or "無"
        lines.append(f"- {r['code']} {r['name']}：{'；'.join(rs.get('pros', []))}"
                     f"｜要注意：{'；'.join(rs.get('cons', [])) or '無'}｜符合的量化條件：{ok}")
    return "\n".join(lines)
