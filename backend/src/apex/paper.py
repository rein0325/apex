"""候選策略的模擬追蹤（紙上交易，docs/持股與每日報告.md §3）。

每個月最後一個交易日收盤後，記錄各候選策略「當天會買哪些股票、各多少比例」，之後用真實行情追蹤結果。
這些策略都**沒有通過驗證期**；追蹤的目的是累積它們沒看過的新資料，幾個月到一兩年後再判斷。

- 只記錄訊號，不用真錢，也不影響持股。
- 訊號日以前的資料才會被策略使用（與回測同一套程式）。為了重現緩衝規則與持股狀態，會用過去約 13 個月重跑
  一次策略，只取最後一天的目標權重，不輸出、不保存任何績效（避免看到保留期的結果）。
- 若記錄時已經過了下一個交易日開盤（例如補記），標示為「補記」，績效統計時另外列出。
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import secrets
from pathlib import Path

import pandas as pd

from apex.db import Database

# 追蹤的候選策略：研究報告中最接近過關的幾組，參數已定版，不再修改
TRACKED = {
    "MLP:lgbm:60:15:3:0.6": "上漲機率 ≥ 60% 才買（防守型）",
    "CS:70:D2&ML:lgbm:60:15:3": "0050 七成（轉弱減碼）＋ML 三成",
    "ML:lgbm:60:15:3": "ML 超額排名前 15 檔",
    "A1:15:3+D3": "趨勢成長 15 檔（Phase 2）",
}
ROUND_TRIP_COST = 0.0078  # 約略來回成本：手續費 0.1425%×2＋證交稅 0.3%＋滑價 0.1%×2

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_runs (
    signal_date DATE, strategy VARCHAR, recorded_at TIMESTAMP, late BOOLEAN, n INTEGER, exposure DOUBLE,
    data_date DATE, code_version VARCHAR, PRIMARY KEY (signal_date, strategy)
);
CREATE TABLE IF NOT EXISTS paper_signals (
    signal_date DATE, strategy VARCHAR, code VARCHAR, weight DOUBLE, PRIMARY KEY (signal_date, strategy, code)
);
ALTER TABLE paper_runs ADD COLUMN IF NOT EXISTS salt VARCHAR;
"""
COMMIT_COLUMNS = ["signal_date", "strategy", "sha256", "recorded_at"]


def ensure_tables(db: Database) -> None:
    db.con.execute(SCHEMA)


def commitment(signal_date: dt.date, strategy: str, weights: dict[str, float], salt: str) -> str:
    """訊號的雜湊承諾：公開雜湊、不公開個股；之後公布 salt 與名單，任何人都能驗證訊號在當時就已記錄。"""
    body = ";".join(f"{c}:{w:.6f}" for c, w in sorted(weights.items()))
    return hashlib.sha256(f"{signal_date}|{strategy}|{body}|{salt}".encode()).hexdigest()


def write_commitments(db: Database, path: Path) -> int:
    """把所有已記錄訊號的雜湊寫到公開檔案（paper/commitments.csv），回傳新增筆數。"""
    ensure_tables(db)
    existing = set()
    if path.exists():
        with path.open(encoding="utf-8", newline="") as fh:
            existing = {(r["signal_date"], r["strategy"]) for r in csv.DictReader(fh)}
    unsalted = db.con.execute("SELECT signal_date, strategy FROM paper_runs WHERE salt IS NULL").fetchall()
    for d, strat in unsalted:
        db.con.execute("UPDATE paper_runs SET salt = ? WHERE signal_date = ? AND strategy = ?",
                       [secrets.token_hex(16), d, strat])  # 加入承諾機制前記錄的訊號
    runs = db.con.execute("SELECT signal_date, strategy, salt, recorded_at FROM paper_runs "
                          "WHERE salt IS NOT NULL ORDER BY signal_date, strategy").fetchall()
    new = []
    for d, strat, salt, at in runs:
        if (str(d), strat) in existing:
            continue
        w = dict(db.con.execute("SELECT code, weight FROM paper_signals "
                                "WHERE signal_date = ? AND strategy = ?",
                                [d, strat]).fetchall())
        new.append([str(d), strat, commitment(d, strat, w, salt), f"{at:%Y-%m-%d %H:%M}"])
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        with path.open("a", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            if fresh:
                w.writerow(COMMIT_COLUMNS)
            w.writerows(new)
    return len(new)


def month_end_signal_days(db: Database, start: dt.date, end: dt.date) -> list[dt.date]:
    """每月最後一個交易日（依交易日曆，未來日期依公布休市日推估）。"""
    days = pd.Series(pd.to_datetime(db.expected_trading_days(start, end)))
    if days.empty:
        return []
    return [d.date() for d in days.groupby(days.dt.to_period("M")).max()]


def latest_signal_day(db: Database, data_date: dt.date) -> dt.date | None:
    """資料日（含）以前最近的月底訊號日；本月月底還沒到就是上個月月底。"""
    start = data_date.replace(day=1) - dt.timedelta(days=40)
    ends = [d for d in month_end_signal_days(db, start, data_date + dt.timedelta(days=40)) if d <= data_date]
    return ends[-1] if ends else None


def next_trading_day(db: Database, date: dt.date) -> dt.date:
    days = db.expected_trading_days(date + dt.timedelta(days=1), date + dt.timedelta(days=20))
    return days[0]


def recorded(db: Database, signal_date: dt.date) -> set[str]:
    ensure_tables(db)
    return {r[0] for r in db.con.execute("SELECT strategy FROM paper_runs WHERE signal_date = ?",
                                         [signal_date]).fetchall()}


def targets(db: Database, signal_date: dt.date, specs: list[str]) -> dict[str, dict[str, float]]:
    """重跑策略到訊號日（只用訊號日以前的資料），回傳各策略在訊號日的目標權重。"""
    from apex.backtest import Context, load, run
    from apex.research.features import Features
    from apex.research.lab import Recorder
    from apex.research.strategies import build

    replay_start = signal_date - dt.timedelta(days=400)
    data = load(db, replay_start, signal_date)
    f = Features(data)
    out = {}
    for spec in specs:
        rec = Recorder(build(spec, f))
        run(data, rec, replay_start, signal_date, 1_000_000, "M")  # 只為了重建持股狀態，結果不使用
        sd = pd.Timestamp(signal_date)
        if sd in data.days:  # 引擎不在最後一天下單，訊號日要另外呼叫一次（狀態接續前一個月）
            rec(Context(sd, data, data.days.get_loc(sd)))
        if sd not in rec.targets:
            raise RuntimeError(f"{spec} 在 {signal_date} 沒有產生訊號（資料不足？）")
        out[spec] = {c: float(w) for c, w in rec.targets[sd].items() if w > 1e-9}
    return out


def record(db: Database, signal_date: dt.date, specs: list[str] | None = None,
           now: dt.datetime | None = None) -> dict[str, dict[str, float]]:
    """記錄尚未記錄的策略訊號；已記錄的不覆蓋（訊號一旦記下就不能改）。"""
    from apex.research.trials import code_version

    ensure_tables(db)
    specs = [s for s in (specs or list(TRACKED)) if s not in recorded(db, signal_date)]
    if not specs:
        return {}
    now = now or dt.datetime.now()
    late = now.date() >= next_trading_day(db, signal_date)
    tg = targets(db, signal_date, specs)
    version = code_version()
    for spec, w in tg.items():
        db.con.execute("INSERT INTO paper_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       [signal_date, spec, now, late, len(w), sum(w.values()), signal_date, version,
                        secrets.token_hex(16)])
        for c, x in w.items():
            db.con.execute("INSERT INTO paper_signals VALUES (?, ?, ?, ?)", [signal_date, spec, c, x])
    return tg


def signals(db: Database, strategy: str) -> pd.DataFrame:
    ensure_tables(db)
    return db.con.execute("""
        SELECT r.signal_date, r.late, s.code, s.weight FROM paper_runs r
        LEFT JOIN paper_signals s USING (signal_date, strategy)
        WHERE r.strategy = ? ORDER BY r.signal_date
    """, [strategy]).df()


def performance(db: Database, strategy: str, benchmark: str = "0050") -> pd.DataFrame:
    """逐期績效：訊號日次一交易日開盤買進，持有到下一個訊號的次一交易日開盤（最後一期算到最新收盤）。

    報酬用還原價（含股利），並扣掉約略的換股成本（換手比例 × 來回成本 ÷ 2）。
    """
    sig = signals(db, strategy)
    if sig.empty:
        return pd.DataFrame()
    runs = sig.groupby("signal_date")["late"].first()
    weights = {d: g.dropna(subset=["code"]).set_index("code")["weight"]
               for d, g in sig.groupby("signal_date")}
    dates = sorted(runs.index)
    codes = sorted({c for w in weights.values() for c in w.index} | {benchmark})
    start = pd.Timestamp(dates[0]).date()
    px = db.con.execute(f"""
        SELECT date, code, adj_open, adj_close FROM daily_quotes_adj
        WHERE code IN ({",".join("?" * len(codes))}) AND date > ?
    """, [*codes, start]).df()
    if px.empty:
        return pd.DataFrame()
    px["date"] = pd.to_datetime(px["date"])
    opn = px.pivot(index="date", columns="code", values="adj_open").sort_index().ffill()
    cls = px.pivot(index="date", columns="code", values="adj_close").reindex(opn.index).ffill()
    return periods(dates, runs, weights, opn, cls, benchmark)


def periods(dates: list, late: pd.Series, weights: dict, opn: pd.DataFrame, cls: pd.DataFrame,
            benchmark: str = "0050") -> pd.DataFrame:
    """逐期報酬（純計算，方便測試）。opn／cls 為還原開盤、收盤價（交易日 × 代號）。"""
    runs = late
    days = opn.index
    rows = []
    prev_w = pd.Series(dtype=float)
    for k, d in enumerate(dates):
        i0 = days.searchsorted(pd.Timestamp(d), side="right")  # 訊號日次一交易日
        if i0 >= len(days):
            break
        entry = opn.iloc[i0]
        if k + 1 < len(dates):
            i1 = days.searchsorted(pd.Timestamp(dates[k + 1]), side="right")
            if i1 >= len(days):
                exit_, end, done = cls.iloc[-1], days[-1], False
            else:
                exit_, end, done = opn.iloc[i1], days[i1], True
        else:
            exit_, end, done = cls.iloc[-1], days[-1], False
        w = weights[d]
        ret = float((w * (exit_[w.index] / entry[w.index] - 1)).fillna(0).sum()) if len(w) else 0.0
        turnover = float(w.sub(prev_w, fill_value=0).abs().sum())
        ret -= turnover * ROUND_TRIP_COST / 2
        bench = float(exit_[benchmark] / entry[benchmark] - 1)
        rows.append({"signal_date": d, "start": days[i0].date(), "end": end.date(), "done": done,
                     "late": bool(runs[d]), "n": int(len(w)), "exposure": float(w.sum()), "return": ret,
                     "bench": bench})
        prev_w = w
    df = pd.DataFrame(rows)
    if not df.empty:
        df["cum"] = (1 + df["return"]).cumprod() - 1
        df["bench_cum"] = (1 + df["bench"]).cumprod() - 1
    return df


def summary_text(db: Database, specs: list[str] | None = None, show_latest: bool = True) -> str:
    specs = specs or list(TRACKED)
    lines = ["模擬追蹤（紙上交易；以下策略都**未通過驗證**，只用來累積新資料，不是買賣建議）", "",
             "| 策略 | 說明 | 期數 | 累積報酬 | 0050 同期 | 最近一期 |", "|---|---|---|---|---|---|"]
    latest = {}
    for s in specs:
        p = performance(db, s)
        if p.empty:
            lines.append(f"| {s} | {TRACKED.get(s, '')} | 0 | — | — | — |")
            continue
        last = p.iloc[-1]
        late = int(p["late"].sum())
        lines.append(f"| {s} | {TRACKED.get(s, '')} | {len(p)}{f'（補記 {late}）' if late else ''} | "
                     f"{last['cum']:+.1%} | {last['bench_cum']:+.1%} | {last['return']:+.1%}"
                     f"{'' if last['done'] else '（進行中）'} |")
        latest[s] = signals(db, s)
    if show_latest and latest:
        lines += ["", "最新一期訊號："]
        names = dict(db.con.execute("SELECT code, name FROM securities").fetchall())
        for s, sig in latest.items():
            d = sig["signal_date"].max()
            g = sig[(sig["signal_date"] == d) & sig["code"].notna()].sort_values("weight", ascending=False)
            picks = "、".join(f"{c} {names.get(c, '')} {w:.0%}"
                             for c, w in zip(g["code"], g["weight"], strict=False))
            cash = 1 - g["weight"].sum()
            lines.append(f"- {s}（{pd.Timestamp(d).date()}）：{picks or '空手'}"
                         f"{f'；現金 {cash:.0%}' if cash > 0.005 else ''}")
    return "\n".join(lines)

