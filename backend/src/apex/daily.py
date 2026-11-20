"""每日報告（本機版，docs/持股與每日報告.md §4）：市場狀態＋持股風險＋模擬追蹤。

報告存在 personal/reports/（只存本機）。網站發布的方式還沒決定（使用者 2026-11-19 暫緩），
之後網站只會放「市場狀態」這一段公開資訊，持股與交易紀錄不會上傳。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from apex.db import Database

REGIME_ADVICE = {
    "多頭": "指數在長期均線之上且均線上揚。",
    "空頭": "指數在長期均線之下且均線下彎，留意持股回撤。",
    "盤整": "趨勢不明確。",
    "高波動": "近期波動處於歷史高檔，價格可能大幅震盪。",
    "資料不足": "資料不足以判斷。",
}


@dataclass
class MarketSummary:
    data_date: dt.date
    expected_date: dt.date  # 依交易日曆，今天以前最近一個應有資料的交易日
    regime: str
    regime_since: dt.date
    ma_gap: float
    ma_slope: float
    vol_pct: float
    breadth: float
    returns: pd.DataFrame  # index：加權報酬指數、0050；columns：近1月、近3月、今年、距一年高點
    history: pd.DataFrame | None = None  # 近一年：加權報酬指數、0050（起點 = 1）與大盤狀態
    industries: pd.DataFrame | None = None  # 產業強弱：產業 → 近1月、近3月（成交金額加權）、檔數
    status: list | None = None  # 各類資料的更新狀態
    otc: dict | None = None  # 櫃買市場狀態（用上櫃股票市值加權自算的指數）

    @property
    def stale(self) -> bool:
        return self.data_date < self.expected_date


def latest_data_date(db: Database) -> dt.date:
    return db.con.execute("""
        SELECT max(date) FROM (SELECT date FROM ingest_status GROUP BY date HAVING bool_and(status = 'ok'))
    """).fetchone()[0]


def market_summary(db: Database, today: dt.date | None = None) -> MarketSummary:
    from apex.backtest import load
    from apex.research.regime import index_series, market_regime

    if today is None:
        now = dt.datetime.now()
        # 收盤資料大約傍晚才齊，18:00 以前「應有資料」只算到前一個交易日
        today = now.date() if now.hour >= 18 else now.date() - dt.timedelta(days=1)
    data_date = latest_data_date(db)
    past = [d for d in db.expected_trading_days(today - dt.timedelta(days=14), today) if d <= today]
    expected = past[-1] if past else data_date
    data = load(db, data_date - dt.timedelta(days=365 * 3 + 60), data_date)
    reg = market_regime(data).dropna(subset=["regime"])
    last = reg.iloc[-1]
    changed = reg["regime"].ne(reg["regime"].shift())
    since = reg.index[changed][-1].date()

    tr = index_series(data, "TAIEX_TR").dropna()
    e50 = data["adj_close"]["0050"].dropna() if "0050" in data["adj_close"] else pd.Series(dtype=float)
    rows = {}
    otc_idx = otc_index(db, data)
    for name, s in (("加權報酬指數", tr), ("0050", e50), ("櫃買股票（自算）", otc_idx)):
        if s.empty:
            continue
        ytd = s[s.index < pd.Timestamp(data_date.year, 1, 1)]
        rows[name] = {
            "近1月": s.iloc[-1] / s.iloc[-22] - 1 if len(s) > 22 else float("nan"),
            "近3月": s.iloc[-1] / s.iloc[-64] - 1 if len(s) > 64 else float("nan"),
            "今年": s.iloc[-1] / ytd.iloc[-1] - 1 if len(ytd) else float("nan"),
            "距一年高點": s.iloc[-1] / s.iloc[-245:].max() - 1,
        }
    industries = industry_strength(data)
    hist = pd.DataFrame({"加權報酬指數": tr, "0050": e50}).iloc[-245:].ffill()
    hist = hist / hist.iloc[0]
    hist["regime"] = reg["regime"].reindex(hist.index)
    return MarketSummary(data_date, expected, str(last["regime"]), since, float(last["ma_gap"]),
                         float(last["ma_slope"]), float(last["vol_pct"]), float(last["breadth"]),
                         pd.DataFrame(rows).T, hist, industries, data_status(db, expected),
                         otc_state(otc_idx, data))


def otc_index(db: Database, data) -> pd.Series:
    """櫃買市場指數（自算）：上櫃普通股依前一日市值加權的還原報酬，串成指數。

    交易所的櫃買指數需要另外的資料來源；這裡用已有的上櫃個股行情與股本自行計算，走勢與櫃買指數相近但不完全相同。
    """
    from apex.research.features import Features

    codes = {r[0] for r in db.con.execute(
        "SELECT DISTINCT code FROM daily_quotes WHERE market = 'TPEX' AND date = ?", [data.days[-1].date()]).fetchall()}
    f = Features(data)
    cols = [c for c in f.stocks if c in codes]
    if not cols:
        return pd.Series(dtype=float)
    w = f["mcap"][cols].shift(1)
    r = f.daily_ret[cols]
    ret = (w * r).sum(axis=1, min_count=1) / w.where(r.notna()).sum(axis=1)
    return (1 + ret.fillna(0)).cumprod().iloc[1:]


def otc_state(idx: pd.Series, data) -> dict | None:
    """櫃買市場的狀態：與大盤相同的分類規則（200 日均線、斜率、波動百分位），寬度用上櫃股票。"""
    from apex.research.regime import classify

    if idx.empty or len(idx) < 260:
        return None
    reg = classify(idx).dropna(subset=["regime"])
    last = reg.iloc[-1]
    changed = reg["regime"].ne(reg["regime"].shift())
    return {"regime": str(last["regime"]), "since": str(reg.index[changed][-1].date()),
            "ma_gap": float(last["ma_gap"]), "vol_pct": float(last["vol_pct"])}


def industry_strength(data, min_members: int = 5) -> pd.DataFrame:
    """各產業近 1 月、近 3 月報酬（同產業可投資普通股依成交金額加權），至少 min_members 檔才列入。"""
    from apex.research.features import Features

    f = Features(data)
    d = data.days[-1]
    ind = f.industry().loc[d]
    r20, r60 = f["ind_ret_20"].loc[d], f["ind_ret_60"].loc[d]
    df = pd.DataFrame({"industry": ind, "近1月": r20, "近3月": r60}).loc[f.stocks].dropna()
    g = df.groupby("industry")
    out = pd.DataFrame({"近1月": g["近1月"].first(), "近3月": g["近3月"].first(), "檔數": g.size()})
    return out[out["檔數"] >= min_members].sort_values("近1月", ascending=False)


def data_status(db: Database, expected: dt.date) -> list[dict]:
    """各類資料是否更新到應有的日期（規格書 §5.4、企劃書 5.3）。"""
    def ok_markets(sql: str, params: list) -> set:
        return {r[0] for r in db.con.execute(sql, params).fetchall()}

    out = []
    quotes = ok_markets("SELECT market FROM ingest_status WHERE date = ? AND status = 'ok'", [expected])
    out.append({"name": "上市、上櫃行情", "date": str(expected), "ok": {"TWSE", "TPEX"} <= quotes})
    for ds, name in (("institutional", "三大法人買賣超"), ("margin", "融資融券")):
        got = ok_markets("SELECT market FROM dataset_status WHERE dataset = ? AND date = ? AND status = 'ok'",
                         [ds, expected])
        last = db.con.execute("SELECT max(date) FROM dataset_status WHERE dataset = ? AND status = 'ok'",
                              [ds]).fetchone()[0]
        out.append({"name": name, "date": str(last) if last else None, "ok": {"TWSE", "TPEX"} <= got})
    idx = db.con.execute("SELECT max(date) FROM index_daily WHERE index_code = 'TAIEX_TR'").fetchone()[0]
    out.append({"name": "加權報酬指數", "date": str(idx) if idx else None, "ok": bool(idx and idx >= expected)})
    today = dt.date.today()
    rev = db.con.execute("SELECT max(year * 100 + month) FROM monthly_revenue").fetchone()[0]
    # 月營收：每月 10 日前公布上個月；11 日起應該要有上個月的資料
    want = today.replace(day=1) - dt.timedelta(days=1 if today.day > 10 else 32)
    out.append({"name": "月營收", "date": f"{rev // 100}-{rev % 100:02d}" if rev else None,
                "ok": bool(rev and rev >= want.year * 100 + want.month), "note": "每月 10 日前公布上個月"})
    fin = db.con.execute("SELECT year, quarter FROM financials_q WHERE available_date <= ? "
                         "ORDER BY year DESC, quarter DESC LIMIT 1", [today]).fetchone()
    out.append({"name": "季財報", "date": f"{fin[0]} 年第 {fin[1]} 季" if fin else None, "ok": fin is not None,
                "note": "依公告期限陸續更新"})
    return out


def market_text(m: MarketSummary) -> str:
    lines = [f"市場狀態｜資料日 {m.data_date}"]
    if m.stale:
        lines.append(f"! 資料不是最新：最近應有資料的交易日是 {m.expected_date}，目前只到 {m.data_date}")
    lines += [
        "",
        f"大盤狀態：**{m.regime}**（自 {m.regime_since} 起）。{REGIME_ADVICE.get(m.regime, '')}",
        f"- 加權指數距 200 日均線 {m.ma_gap:+.1%}，均線近 20 日變化 {m.ma_slope:+.2%}",
        f"- 近 20 日波動在過去三年的百分位 {m.vol_pct:.0%}",
        f"- 站上 60 日均線的股票比例 {m.breadth:.0%}",
        "",
        "| | 近1月 | 近3月 | 今年 | 距一年高點 |",
        "|---|---|---|---|---|",
    ]
    for name, r in m.returns.iterrows():
        cells = " | ".join(f"{r[c]:+.1%}" for c in ("近1月", "近3月", "今年", "距一年高點"))
        lines.append(f"| {name} | {cells} |")
    if m.industries is not None and len(m.industries):
        top, bottom = m.industries.head(5), m.industries.tail(5).iloc[::-1]
        fmt = lambda df: "、".join(f"{i} {r['近1月']:+.0%}" for i, r in df.iterrows())  # noqa: E731
        lines += ["", f"近 1 個月最強產業：{fmt(top)}", f"近 1 個月最弱產業：{fmt(bottom)}"]
    if m.otc:
        lines += ["", f"櫃買市場（自算）：**{m.otc['regime']}**（自 {m.otc['since']} 起），距 200 日均線 {m.otc['ma_gap']:+.1%}"]
    if m.status:
        bad = [x["name"] for x in m.status if not x["ok"]]
        lines += ["", "資料更新狀態：" + ("全部正常" if not bad else "未更新：" + "、".join(bad))]
    return "\n".join(lines)


def compose(sections: list[str], generated: dt.datetime | None = None) -> str:
    generated = generated or dt.datetime.now()
    head = [f"# 每日報告 {generated:%Y-%m-%d}", "",
            f"產生時間 {generated:%Y-%m-%d %H:%M}｜僅供參考，不構成投資建議；過去績效不代表未來。", ""]
    return "\n".join(head) + "\n\n---\n\n".join(s.strip() for s in sections if s) + "\n"
