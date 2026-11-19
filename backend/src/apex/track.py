"""候選股票的預測紀錄與實際成績（產品企劃書 3.8「預測與實際報酬比較」、規格書 STEP 10 模型健康）。

每天把候選名單（排名、分數等級、當時的預期）記下來，之後用真實行情計算：
- 已到期（滿 60 個交易日）的推薦：實際上漲比例、平均與中間值，和當時預期的上漲機率比較。
- 還沒到期的推薦：到目前為止的報酬，和同期間 0050 比較。
- 模型健康：到期樣本夠多時，實際上漲比例明顯低於「隨便挑」的比例，就提醒模型可能失準。

報酬計算與回測一致：推薦日的下一個交易日開盤買進，持有 h 個交易日後開盤賣出（還原價，含股利），不扣成本。
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from apex.db import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS pick_log (
    signal_date DATE, code VARCHAR, rank INTEGER, pct DOUBLE, bucket VARCHAR, close DOUBLE,
    up DOUBLE, q50 DOUBLE, mean DOUBLE, model VARCHAR, trained_until DATE, code_version VARCHAR,
    recorded_at TIMESTAMP, PRIMARY KEY (signal_date, code)
);
"""
TOP = 10  # 成績只看每天前 10 名（網站預設會看到的部分）
MIN_MATURED = 50  # 到期樣本至少這麼多才判斷模型健康


def ensure(db: Database) -> None:
    db.con.execute(SCHEMA)


def record(db: Database, cands: dict) -> int:
    """記錄當天的候選名單；同一天已記錄就不覆蓋（推薦一旦記下就不改）。回傳新增筆數。"""
    from apex.research.trials import code_version

    ensure(db)
    d = cands["data_date"]
    if db.con.execute("SELECT count(*) FROM pick_log WHERE signal_date = ?", [d]).fetchone()[0]:
        return 0
    h = cands["horizons"][0]
    now, version = dt.datetime.now(), code_version()
    rows = []
    for r in cands["candidates"]:
        x = r.get(f"h{h}") or {}
        rows.append([d, r["code"], r["rank"], r.get("pct"), x.get("bucket"), r["close"], x.get("up"),
                     x.get("q50"), x.get("mean"), cands["model"], cands["trained_until"], version, now])
    db.con.executemany("INSERT INTO pick_log VALUES (" + ", ".join("?" * 13) + ")", rows)
    return len(rows)


def _prices(db: Database, codes: list[str], start: dt.date) -> tuple[pd.DataFrame, pd.DataFrame]:
    px = db.con.execute(f"""
        SELECT date, code, adj_open, adj_close FROM daily_quotes_adj
        WHERE code IN ({",".join("?" * len(codes))}) AND date > ?
    """, [*codes, start]).df()
    if px.empty:
        return pd.DataFrame(), pd.DataFrame()
    px["date"] = pd.to_datetime(px["date"])
    opn = px.pivot(index="date", columns="code", values="adj_open").sort_index().ffill()
    cls = px.pivot(index="date", columns="code", values="adj_close").reindex(opn.index).ffill()
    return opn, cls


def outcomes(log: pd.DataFrame, opn: pd.DataFrame, cls: pd.DataFrame, horizon: int = 60,
             bench: str = "0050") -> pd.DataFrame:
    """每筆推薦的結果（純計算）：到期的用第 h 個交易日開盤價，未到期的用最新收盤價。"""
    days = opn.index
    rows = []
    for d, g in log.groupby("signal_date"):
        i0 = days.searchsorted(pd.Timestamp(d), side="right")
        if i0 >= len(days):
            continue
        i1 = i0 + horizon
        matured = i1 < len(days)
        exit_ = opn.iloc[i1] if matured else cls.iloc[-1]
        held = (i1 if matured else len(days) - 1) - i0
        entry = opn.iloc[i0]
        b = float(exit_.get(bench, np.nan) / entry.get(bench, np.nan) - 1) if bench in opn else np.nan
        for _, r in g.iterrows():
            c = r["code"]
            ret = float(exit_.get(c, np.nan) / entry.get(c, np.nan) - 1) if c in opn else np.nan
            rows.append({"signal_date": pd.Timestamp(d).date(), "code": c, "rank": int(r["rank"]),
                         "matured": matured, "days": int(held), "ret": ret, "bench": b,
                         "up": r["up"], "q50": r["q50"]})
    return pd.DataFrame(rows)


def track_record(db: Database, horizon: int = 60, base_up: float | None = None) -> dict:
    """候選股票的實際成績摘要（網站與每日報告用）。base_up：隨便挑一檔的歷史上漲機率。"""
    ensure(db)
    log = db.con.execute("SELECT * FROM pick_log WHERE rank <= ? ORDER BY signal_date, rank", [TOP]).df()
    if log.empty:
        return {"since": None, "signals": 0, "matured": None, "recent": [],
                "health": {"status": "累積中", "msg": "今天開始記錄，之後會顯示推薦的實際成績。"}}
    start = pd.Timestamp(log["signal_date"].min()).date()
    opn, cls = _prices(db, sorted(set(log["code"]) | {"0050"}), start)
    out = outcomes(log, opn, cls, horizon) if not opn.empty else pd.DataFrame()
    res: dict = {"since": str(start), "signals": int(log["signal_date"].nunique()), "horizon": horizon,
                 "top": TOP, "matured": None, "recent": []}
    if out.empty:
        res["health"] = {"status": "累積中", "msg": "推薦後還沒有交易日，明天開始有成績。"}
        return res
    done = out[out["matured"] & out["ret"].notna()]
    if len(done):
        res["matured"] = {"n": int(len(done)), "dates": int(done["signal_date"].nunique()),
                          "hit": float((done["ret"] > 0).mean()), "mean": float(done["ret"].mean()),
                          "median": float(done["ret"].median()), "bench": float(done.groupby("signal_date")["bench"].first().mean()),
                          "expected_up": float(done["up"].mean()), "expected_q50": float(done["q50"].mean())}
    for d, g in list(out.groupby("signal_date"))[-10:][::-1]:
        res["recent"].append({"date": str(d), "days": int(g["days"].iloc[0]), "matured": bool(g["matured"].iloc[0]),
                              "mean": float(g["ret"].mean()), "hit": float((g["ret"] > 0).mean()),
                              "bench": float(g["bench"].iloc[0])})
    m = res["matured"]
    if m is None or m["n"] < MIN_MATURED:
        n = 0 if m is None else m["n"]
        res["health"] = {"status": "累積中", "msg": f"已到期的推薦 {n} 筆，滿 {MIN_MATURED} 筆後才判斷模型準不準"
                                                   f"（第一批約在推薦後 {horizon} 個交易日、約 3 個月到期）。"}
    else:
        base = base_up if base_up is not None else 0.48
        se = np.sqrt(base * (1 - base) / m["n"])
        if m["hit"] < base - 2 * se:
            res["health"] = {"status": "失準", "msg": f"實際上漲比例 {m['hit']:.0%}，明顯低於隨便挑的 {base:.0%}，"
                                                     "模型最近可能失靈，建議暫停參考。"}
        elif m["hit"] < m["expected_up"] - 2 * se:
            res["health"] = {"status": "注意", "msg": f"實際上漲比例 {m['hit']:.0%}，低於當時預期的 {m['expected_up']:.0%}。"}
        else:
            res["health"] = {"status": "正常", "msg": f"實際上漲比例 {m['hit']:.0%}，當時預期 {m['expected_up']:.0%}。"}
    return res


def text(t: dict) -> str:
    lines = ["候選股票的實際成績", "", f"狀態：{t['health']['status']}｜{t['health']['msg']}"]
    if t.get("matured"):
        m = t["matured"]
        lines.append(f"已到期 {m['n']} 筆：上漲 {m['hit']:.0%}（預期 {m['expected_up']:.0%}）、"
                     f"平均 {m['mean']:+.1%}、中間值 {m['median']:+.1%}；同期 0050 平均 {m['bench']:+.1%}")
    for r in t.get("recent", [])[:5]:
        lines.append(f"- {r['date']} 推薦前 {t.get('top', TOP)} 名，持有 {r['days']} 天"
                     f"{'（已到期）' if r['matured'] else ''}：平均 {r['mean']:+.1%}、上漲 {r['hit']:.0%}；"
                     f"0050 {r['bench']:+.1%}")
    return "\n".join(lines)
