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
    for name, s in (("加權報酬指數", tr), ("0050", e50)):
        if s.empty:
            continue
        ytd = s[s.index < pd.Timestamp(data_date.year, 1, 1)]
        rows[name] = {
            "近1月": s.iloc[-1] / s.iloc[-22] - 1 if len(s) > 22 else float("nan"),
            "近3月": s.iloc[-1] / s.iloc[-64] - 1 if len(s) > 64 else float("nan"),
            "今年": s.iloc[-1] / ytd.iloc[-1] - 1 if len(ytd) else float("nan"),
            "距一年高點": s.iloc[-1] / s.iloc[-245:].max() - 1,
        }
    hist = pd.DataFrame({"加權報酬指數": tr, "0050": e50}).iloc[-245:].ffill()
    hist = hist / hist.iloc[0]
    hist["regime"] = reg["regime"].reindex(hist.index)
    return MarketSummary(data_date, expected, str(last["regime"]), since, float(last["ma_gap"]),
                         float(last["ma_slope"]), float(last["vol_pct"]), float(last["breadth"]),
                         pd.DataFrame(rows).T, hist)


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
    return "\n".join(lines)


def compose(sections: list[str], generated: dt.datetime | None = None) -> str:
    generated = generated or dt.datetime.now()
    head = [f"# 每日報告 {generated:%Y-%m-%d}", "",
            f"產生時間 {generated:%Y-%m-%d %H:%M}｜僅供參考，不構成投資建議；過去績效不代表未來。", ""]
    return "\n".join(head) + "\n\n---\n\n".join(s.strip() for s in sections if s) + "\n"
