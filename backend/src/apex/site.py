"""公開網站的資料（docs/網站與每日自動化.md）：後端只產生 data.json，畫面在 repo 根目錄的 frontend/。

`build` 把 frontend/ 的靜態檔案複製到輸出目錄，再寫入當天的 data.json，整個目錄就是 GitHub Pages 的內容。

不放：個人持股與交易紀錄（只存本機）、模擬追蹤的個股清單與策略規格（未持有投顧執照，不公開推薦個股）。
放：大盤狀態與指標、加權報酬指數與 0050 的近期表現、資料日期與更新狀態、模擬追蹤的整體績效。
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import pandas as pd

from apex.config import REPO_ROOT
from apex.daily import MarketSummary

FRONTEND_DIR = REPO_ROOT / "frontend"


def _num(x) -> float | None:
    return None if x is None or pd.isna(x) else round(float(x), 6)


def paper_rows(perf: dict[str, pd.DataFrame], descriptions: dict[str, str]) -> list[dict]:
    """模擬追蹤整體績效：只有策略說明與報酬，不含個股與策略規格。"""
    rows = []
    for spec, p in perf.items():
        name = descriptions.get(spec, "研究策略")
        if p.empty:
            rows.append({"name": name, "periods": 0})
            continue
        last = p.iloc[-1]
        rows.append({"name": name, "periods": int(len(p)), "since": str(p.iloc[0]["start"]),
                     "cum": _num(last["cum"]), "bench_cum": _num(last["bench_cum"]),
                     "exposure": _num(last["exposure"])})
    return rows


def data(m: MarketSummary, paper: list[dict], generated: dt.datetime) -> dict:
    h = m.history
    history = None
    if h is not None and not h.empty:
        history = {"dates": [f"{d:%Y-%m-%d}" for d in h.index],
                   "tr": [_num(v) for v in h.get("加權報酬指數", pd.Series(dtype=float))],
                   "e50": [_num(v) for v in h["0050"]] if "0050" in h else None,
                   "regime": [None if pd.isna(v) else str(v) for v in h["regime"]]}
    return {
        "data_date": str(m.data_date), "expected_date": str(m.expected_date), "stale": m.stale,
        "generated": generated.isoformat(timespec="minutes"),
        "regime": m.regime, "regime_since": str(m.regime_since),
        "ma_gap": _num(m.ma_gap), "ma_slope": _num(m.ma_slope), "vol_pct": _num(m.vol_pct),
        "breadth": _num(m.breadth),
        "returns": {str(k): {c: _num(v) for c, v in r.items()} for k, r in m.returns.iterrows()},
        "history": history,
        "paper": paper,
    }


def price_data(db, data_date: dt.date, lookback_days: int = 60) -> dict:
    """「我的持股」頁用的公開行情：各證券最新收盤價，以及除權息、分割等事件的還原比例。

    還原價以最新一天為基準（最新的還原因子 = 1）。事件比例 = 事件前因子 ÷ 事件後因子，
    瀏覽器用「交易日之後所有事件比例的乘積」把當時的股數換算成今天的約當股數（現金股利視為再投入）。
    這些都是交易所的公開資料，不含任何個人資訊。
    """
    last = db.con.execute("""
        SELECT q.code, s.name, s.sec_type, q.close, q.date
        FROM daily_quotes_adj q JOIN securities s USING (code)
        WHERE q.date BETWEEN ? AND ? AND q.close > 0
        QUALIFY row_number() OVER (PARTITION BY q.code ORDER BY q.date DESC) = 1
    """, [data_date - dt.timedelta(days=lookback_days), data_date]).fetchall()
    stocks = {}
    for code, name, sec_type, close, date in last:
        row = [name, sec_type, round(float(close), 4)]
        if date < data_date:
            row.append(str(date))  # 最後成交日（可能停止交易或下市）
        stocks[code] = row
    events: dict[str, list] = {}
    for code, date, ratio in db.con.execute("""
        WITH f AS (
            SELECT code, date, adj_close / close AS f,
                   lag(adj_close / close) OVER (PARTITION BY code ORDER BY date) AS pf
            FROM daily_quotes_adj WHERE close > 0 AND adj_close > 0
        )
        SELECT code, date, pf / f FROM f WHERE pf IS NOT NULL AND abs(f / pf - 1) > 1e-6 ORDER BY code, date
    """).fetchall():
        if code in stocks:
            events.setdefault(code, []).append([str(date), round(float(ratio), 8)])
    return {"date": str(data_date), "stocks": stocks, "events": events}


def build(m: MarketSummary, paper: list[dict], out_dir: Path, generated: dt.datetime | None = None,
          frontend: Path = FRONTEND_DIR, prices: dict | None = None) -> Path:
    """輸出網站目錄：frontend/ 的檔案＋data.json（＋prices.json）。回傳 data.json 路徑。"""
    generated = generated or dt.datetime.now()
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in frontend.iterdir():
        if f.is_file():
            shutil.copy2(f, out_dir / f.name)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    if prices is not None:
        (out_dir / "prices.json").write_text(json.dumps(prices, ensure_ascii=False, separators=(",", ":")),
                                             encoding="utf-8")
    path = out_dir / "data.json"
    path.write_text(json.dumps(data(m, paper, generated), ensure_ascii=False), encoding="utf-8")
    return path
