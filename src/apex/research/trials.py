"""試驗紀錄與 Deflated Sharpe Ratio（docs/策略研究計畫.md §2）。

每跑一組策略參數就寫一筆，不刪除。最後報告列出試驗總數，並用 Deflated Sharpe Ratio
（Bailey & López de Prado, 2014）估計「在試了 N 次之後，最佳結果仍為真」的機率。
"""

from __future__ import annotations

import json
import math
import subprocess
from statistics import NormalDist

import numpy as np
import pandas as pd

from apex.db import Database

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS research_trials_seq;
CREATE TABLE IF NOT EXISTS research_trials (
    id INTEGER PRIMARY KEY DEFAULT nextval('research_trials_seq'),
    run_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    family VARCHAR,            -- 策略類別：A 趨勢成長、B 強勢輪動、C 成長潛力、D 風險調整、E 對照、base 基準
    strategy VARCHAR NOT NULL, -- 策略規格字串，例如 'equal:50'
    period VARCHAR NOT NULL,   -- research / validation / holdout / custom
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    freq VARCHAR NOT NULL,
    capital DOUBLE NOT NULL,
    costs VARCHAR,             -- JSON
    cagr DOUBLE,
    sharpe DOUBLE,
    max_drawdown DOUBLE,
    daily_sr DOUBLE,           -- 日報酬 Sharpe（未年化），供 DSR 使用
    skew DOUBLE,
    kurtosis DOUBLE,           -- 非超額峰度
    n_days INTEGER,
    metrics VARCHAR,           -- JSON：完整指標
    code_version VARCHAR,
    note VARCHAR
);
"""
EULER = 0.5772156649
N = NormalDist()


def ensure(db: Database) -> None:
    db.con.execute(SCHEMA)


def code_version() -> str:
    try:
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                           check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "src"], capture_output=True, text=True,
                               check=True).stdout.strip()
        return h + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def return_moments(equity: pd.Series) -> dict[str, float]:
    r = equity.pct_change().dropna()
    sd = r.std()
    return {"daily_sr": float(r.mean() / sd) if sd > 0 else float("nan"), "skew": float(r.skew()),
            "kurtosis": float(r.kurt() + 3), "n_days": int(len(r))}


def record(db: Database, *, family: str, strategy: str, period: str, start, end, freq: str, capital: float,
           costs: dict, metrics: dict, equity: pd.Series, note: str = "") -> int:
    ensure(db)
    mom = return_moments(equity)
    clean = {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in metrics.items()}
    return db.con.execute("""
        INSERT INTO research_trials (family, strategy, period, start_date, end_date, freq, capital, costs,
            cagr, sharpe, max_drawdown, daily_sr, skew, kurtosis, n_days, metrics, code_version, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
    """, [family, strategy, period, start, end, freq, capital, json.dumps(costs), metrics.get("cagr"),
          metrics.get("sharpe"), metrics.get("max_drawdown"), mom["daily_sr"], mom["skew"], mom["kurtosis"],
          mom["n_days"], json.dumps(clean, default=float), code_version(), note]).fetchone()[0]


def expected_max_sr(n_trials: int, sr_var: float) -> float:
    """N 次獨立試驗、每次真實 Sharpe 為 0 時，最佳 Sharpe 的期望值（日報酬單位）。"""
    if n_trials <= 1 or sr_var <= 0:
        return 0.0
    z1 = N.inv_cdf(1 - 1 / n_trials)
    z2 = N.inv_cdf(1 - 1 / (n_trials * math.e))
    return math.sqrt(sr_var) * ((1 - EULER) * z1 + EULER * z2)


def deflated_sharpe(sr: float, n_days: int, skew: float, kurtosis: float, n_trials: int,
                    sr_var: float) -> float:
    """DSR：考慮試驗次數、樣本長度與報酬分布後，Sharpe 真正大於 0 的機率（0～1）。"""
    sr0 = expected_max_sr(n_trials, sr_var)
    denom = 1 - skew * sr + (kurtosis - 1) / 4 * sr ** 2
    if n_days < 2 or denom <= 0:
        return float("nan")
    return N.cdf((sr - sr0) * math.sqrt(n_days - 1) / math.sqrt(denom))


def trials(db: Database, period: str | None = "research", family: str | None = None) -> pd.DataFrame:
    ensure(db)
    sql = "SELECT * FROM research_trials WHERE 1=1"
    args: list = []
    if period:
        sql += " AND period = ?"
        args.append(period)
    if family:
        sql += " AND family = ?"
        args.append(family)
    return db.con.execute(sql + " ORDER BY id", args).df()


def deflate(db: Database, trial_id: int, period: str = "research") -> dict[str, float]:
    """以同期間所有非基準試驗的 Sharpe 變異數與試驗數，計算某一筆試驗的 DSR。"""
    df = trials(db, period)
    df = df[df["family"] != "base"].drop_duplicates(["strategy", "freq", "capital", "costs"], keep="last")
    row = trials(db, None).set_index("id").loc[trial_id]
    n = max(len(df), 1)
    var = float(np.nanvar(df["daily_sr"], ddof=1)) if n > 1 else 0.0
    return {"n_trials": n, "sr_annual": row["daily_sr"] * math.sqrt(245),
            "expected_max_sr_annual": expected_max_sr(n, var) * math.sqrt(245),
            "dsr": deflated_sharpe(row["daily_sr"], int(row["n_days"]), row["skew"], row["kurtosis"], n, var)}
