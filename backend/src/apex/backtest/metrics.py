"""績效指標（系統規格書 §4）。"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 245  # 台股每年約 240～250 個交易日


def drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1


def summarize(equity: pd.Series, trades: pd.DataFrame | None = None, benchmark: pd.Series | None = None,
              cash: pd.Series | None = None, rf: float = 0.0) -> dict[str, float]:
    """rf：年化無風險利率。benchmark：同期間的基準淨值（會對齊到 equity 的日期）。"""
    rets = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else float("nan")
    daily_rf = (1 + rf) ** (1 / TRADING_DAYS) - 1
    excess = rets - daily_rf
    vol = rets.std() * math.sqrt(TRADING_DAYS)
    downside = excess[excess < 0]
    downside_dev = (math.sqrt((downside ** 2).sum() / len(excess)) * math.sqrt(TRADING_DAYS)
                    if len(excess) else 0.0)
    dd = drawdown(equity)
    out = {
        "total_return": total,
        "cagr": cagr,
        "volatility": vol,
        "sharpe": excess.mean() / rets.std() * math.sqrt(TRADING_DAYS) if rets.std() > 0 else float("nan"),
        "sortino": excess.mean() * TRADING_DAYS / downside_dev if downside_dev > 0 else float("nan"),
        "max_drawdown": dd.min(),
        "max_drawdown_days": _longest_underwater(dd),
        "calmar": cagr / -dd.min() if dd.min() < 0 else float("nan"),
    }
    if cash is not None:
        out["exposure"] = float((1 - cash / equity).mean())
    if benchmark is not None:
        b = benchmark.reindex(equity.index).ffill()
        b_total = b.iloc[-1] / b.iloc[0] - 1
        b_cagr = (1 + b_total) ** (1 / years) - 1 if years > 0 else float("nan")
        out["benchmark_cagr"] = b_cagr
        out["excess_cagr"] = cagr - b_cagr
        active = rets - b.pct_change().reindex(rets.index)
        out["information_ratio"] = (active.mean() / active.std() * math.sqrt(TRADING_DAYS)
                                    if active.std() > 0 else float("nan"))
    if trades is not None:
        out.update(trade_stats(trades, equity))
    return out


def _longest_underwater(dd: pd.Series) -> int:
    longest = run = 0
    for v in dd.to_numpy():
        run = run + 1 if v < 0 else 0
        longest = max(longest, run)
    return longest


def trade_stats(trades: pd.DataFrame, equity: pd.Series) -> dict[str, float]:
    """以每檔股票「建立部位到清空」為一筆完整交易，計算勝率與盈虧比。成本含手續費與稅。"""
    if trades.empty:
        return {"n_trades": 0, "turnover": 0.0, "total_costs": 0.0}
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    costs = float(trades["commission"].sum() + trades["tax"].sum())
    turnover = float(trades["value"].sum() / 2 / equity.mean() / years) if years > 0 else float("nan")
    pnl = []
    holding_days = []
    for _, g in trades.sort_values("date", kind="stable").groupby("code"):
        shares, cost, opened = 0.0, 0.0, None
        for r in g.itertuples():
            if r.side == "buy":
                if shares <= 1e-9:
                    opened, cost = r.date, 0.0
                shares += r.shares
                cost += r.value + r.commission
            else:
                frac = min(1.0, r.shares / shares) if shares > 0 else 1.0
                proceeds = r.value - r.commission - r.tax
                basis = cost * frac
                cost -= basis
                shares -= r.shares
                if shares <= 1e-6:
                    pnl.append(proceeds - basis + 0)
                    holding_days.append((pd.Timestamp(r.date) - pd.Timestamp(opened)).days if opened else 0)
                    shares, cost = 0.0, 0.0
                else:
                    pnl.append(proceeds - basis)
    pnl_arr = np.array(pnl)
    wins, losses = pnl_arr[pnl_arr > 0], pnl_arr[pnl_arr < 0]
    return {
        "n_trades": int(len(trades)),
        "n_round_trips": len(holding_days),
        "win_rate": float(len(wins) / len(pnl_arr)) if len(pnl_arr) else float("nan"),
        "profit_factor": (float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0
                          else float("nan")),
        "avg_win_loss": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else float("nan"),
        "avg_holding_days": float(np.mean(holding_days)) if holding_days else float("nan"),
        "turnover": turnover,
        "total_costs": costs,
        "cost_drag": costs / equity.mean() / years if years > 0 else float("nan"),
    }


def yearly_returns(equity: pd.Series, benchmark: pd.Series | None = None) -> pd.DataFrame:
    def by_year(s: pd.Series) -> pd.Series:
        ye = s.groupby(s.index.year).last()
        first = s.iloc[0]
        return ye / ye.shift(1, fill_value=first) - 1

    out = pd.DataFrame({"strategy": by_year(equity)})
    if benchmark is not None:
        out["benchmark"] = by_year(benchmark.reindex(equity.index).ffill())
        out["excess"] = out["strategy"] - out["benchmark"]
    return out
