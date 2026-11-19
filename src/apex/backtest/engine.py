"""回測引擎：目標權重再平衡、隔日開盤成交、交易成本與可交易性限制。

時間順序（每個交易日 t）：
1. 以 t 的開盤價執行前一個訊號日產生的委託（先賣後買，現金不足時按比例縮減買單）。
2. 以 t 的收盤價計算持股市值。
3. 若 t 是再平衡日，策略只能看到 t（含）以前的資料，產生目標權重，委託留到 t+1 開盤執行。

持股以「還原股數」記錄：市值＝還原股數 × 還原收盤價，等於股利自動再投入。
整張限制、手續費、成交量上限以未還原價格計算。
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from apex.backtest.costs import CostModel
from apex.backtest.data import MarketData


@dataclass
class Context:
    """策略在訊號日可以看到的資料。history 只回傳到訊號日（含）為止。"""

    date: pd.Timestamp
    data: MarketData
    _i: int

    def history(self, field: str, lookback: int | None = None) -> pd.DataFrame:
        panel = self.data[field]
        start = 0 if lookback is None else max(0, self._i + 1 - lookback)
        return panel.iloc[start:self._i + 1]

    def tradable(self) -> pd.Index:
        """訊號日有成交的代號。"""
        row = self.data["close"].iloc[self._i]
        return row.index[row.notna()]


# 策略：輸入 Context，回傳 {代號: 目標權重}；權重和 ≤ 1，其餘為現金。
Strategy = Callable[[Context], dict[str, float]]


@dataclass
class Trade:
    date: dt.date
    code: str
    side: str  # 'buy' | 'sell'
    shares: float  # 未還原股數
    price: float  # 成交價（含滑價，未還原）
    value: float
    commission: float
    tax: float
    note: str = ""


@dataclass
class BacktestResult:
    equity: pd.Series  # 每日收盤後總資產
    cash: pd.Series
    trades: pd.DataFrame
    events: list[str] = field(default_factory=list)  # 未成交、下市等事件紀錄
    config: dict = field(default_factory=dict)


def rebalance_dates(days: pd.DatetimeIndex, freq: str) -> set[pd.Timestamp]:
    """訊號日：'M' 每月最後一個交易日、'W' 每週最後一個交易日、'D' 每天、'once' 只在第一天。"""
    if freq == "D":
        return set(days)
    if freq == "once":
        return {days[0]}
    key = days.to_period("M" if freq == "M" else "W")
    s = pd.Series(days, index=days)
    return set(s.groupby(key).max())


def run(data: MarketData, strategy: Strategy, start: dt.date, end: dt.date, capital: float = 1_000_000,
        freq: str = "M", costs: CostModel | None = None) -> BacktestResult:
    costs = costs or CostModel()
    days = data.days[(data.days >= pd.Timestamp(start)) & (data.days <= pd.Timestamp(end))]
    if len(days) < 2:
        raise ValueError("回測期間交易日不足")
    signal_days = rebalance_dates(days, freq)
    signal_days.add(days[0])  # 第一天收盤建立部位

    c, ao, ac = data["close"], data["adj_open"], data["adj_close"]
    pos_i = {d: i for i, d in enumerate(data.days)}
    sec = data.securities

    cash = float(capital)
    units: dict[str, float] = {}  # 還原股數
    last_adj: dict[str, float] = {}  # 最後一個有成交日的還原收盤價（停止買賣時用來估值）
    pending: dict[str, float] | None = None  # 目標權重，待下一日開盤執行
    trades: list[Trade] = []
    events: list[str] = []
    equity_s, cash_s = [], []

    for t in days:
        i = pos_i[t]
        today = t.date()

        # 0. 下市：最後交易日之後到終止日，持股凍結；終止日起依 delist_recovery 轉為現金
        for code in list(units):
            if not math.isnan(c.iat[i, c.columns.get_loc(code)]):
                continue
            info = sec.loc[code] if code in sec.index else None
            if info is None or info["status"] == "listed" or pd.isna(info["last_date"]):
                continue
            end_date = info["delisted_date"] if info["status"] == "delisted" else info["last_date"]
            if pd.notna(end_date) and today > info["last_date"] and today >= end_date:
                value = units.pop(code) * last_adj[code] * costs.delist_recovery
                cash += value
                events.append(f"{today} {code} {'下市' if info['status'] == 'delisted' else '停止交易'}，"
                              f"以最後收盤價的 {costs.delist_recovery:.0%} 轉為現金 {value:,.0f}")

        # 1. 開盤執行委託
        if pending is not None:
            equity_open = cash + sum(u * _px(ao, i, code, last_adj) for code, u in units.items())
            cash = _execute(pending, t, i, data, units, last_adj, cash, equity_open, costs, trades, events)
            pending = None

        # 2. 收盤估值
        for code in units:
            p = ac.iat[i, ac.columns.get_loc(code)]
            if not math.isnan(p):
                last_adj[code] = p
        equity = cash + sum(u * last_adj[code] for code, u in units.items())
        equity_s.append(equity)
        cash_s.append(cash)

        # 3. 訊號
        if t in signal_days and t != days[-1]:
            weights = strategy(Context(t, data, i))
            total = sum(weights.values())
            if total > 1 + 1e-9:
                raise ValueError(f"{today} 目標權重和 {total:.3f} > 1")
            pending = {k: v for k, v in weights.items() if v > 0}

    trades_df = pd.DataFrame([vars(x) for x in trades], columns=list(Trade.__dataclass_fields__))
    return BacktestResult(pd.Series(equity_s, index=days, name="equity"), pd.Series(cash_s, index=days),
                          trades_df, events,
                          {"start": str(start), "end": str(end), "capital": capital, "freq": freq,
                           "costs": vars(costs)})


def _px(panel: pd.DataFrame, i: int, code: str, fallback: dict[str, float]) -> float:
    p = panel.iat[i, panel.columns.get_loc(code)] if code in panel.columns else float("nan")
    return fallback.get(code, 0.0) if math.isnan(p) else p


def _execute(target: dict[str, float], t: pd.Timestamp, i: int, data: MarketData, units: dict[str, float],
             last_adj: dict[str, float], cash: float, equity: float, costs: CostModel, trades: list[Trade],
             events: list[str]) -> float:
    o, ao, ac, val = data["open"], data["adj_open"], data["adj_close"], data["value"]
    today = t.date()
    sec_types = data.securities["sec_type"]

    def quote(code: str) -> tuple[float, float, float, float] | None:
        if code not in o.columns:
            return None
        j = o.columns.get_loc(code)
        raw, adj, value = o.iat[i, j], ao.iat[i, j], val.iat[i, j]
        if math.isnan(raw) or math.isnan(adj) or raw <= 0:
            return None
        prev = ac.iloc[:i, j].dropna()
        move = adj / prev.iat[-1] - 1 if len(prev) else 0.0
        return raw, adj, (value if not np.isnan(value) else 0.0), move

    codes = set(target) | set(units)
    deltas = {}
    for code in codes:
        q = quote(code)
        current = units.get(code, 0.0) * (q[1] if q else last_adj.get(code, 0.0))
        deltas[code] = (target.get(code, 0.0) * equity - current, q)

    # 先賣
    for code, (delta, q) in sorted(deltas.items(), key=lambda x: x[1][0]):
        if delta >= 0:
            continue
        if q is None:
            events.append(f"{today} {code} 無成交，賣單未執行")
            continue
        raw, adj, day_value, move = q
        if move <= -costs.limit_threshold:
            events.append(f"{today} {code} 開盤跌停（{move:.1%}），賣單未執行")
            continue
        sell_value = min(-delta, units[code] * adj, day_value * costs.max_participation)
        shares = _round_lot(sell_value / raw, costs.lot_size)
        selling_all = target.get(code, 0.0) == 0 and sell_value >= units[code] * adj - 1e-6
        if selling_all:
            shares = units[code] * adj / raw  # 全數賣出（含零股）
        if shares <= 0:
            continue
        price = costs.sell_price(raw)
        value = shares * price
        fee, tax = costs.commission(value), costs.tax(value, sec_types.get(code, "stock"), code)
        cash += value - fee - tax
        units[code] -= shares * raw / adj
        if selling_all or units[code] * adj < 1e-6:
            units.pop(code)
        trades.append(Trade(today, code, "sell", shares, price, value, fee, tax))

    # 再買：現金不足時按比例縮減
    buys = {code: (delta, q) for code, (delta, q) in deltas.items() if delta > 0}
    need = sum(d for d, _ in buys.values())
    fee_rate = costs.commission_rate * costs.commission_discount
    scale = min(1.0, max(cash, 0.0) / (need * (1 + costs.slippage_bps / 10_000 + fee_rate))) if need else 0
    for code, (delta, q) in sorted(buys.items(), key=lambda x: -x[1][0]):
        if q is None:
            events.append(f"{today} {code} 無成交，買單未執行")
            continue
        raw, adj, day_value, move = q
        if move >= costs.limit_threshold:
            events.append(f"{today} {code} 開盤漲停（{move:+.1%}），買單未執行")
            continue
        price = costs.buy_price(raw)
        buy_value = min(delta * scale, day_value * costs.max_participation)
        shares = _round_lot(buy_value / price, costs.lot_size)
        value = shares * price
        fee = costs.commission(value)
        if value + fee > cash:  # 最低手續費等造成超出現金：縮減股數
            shares = _round_lot((cash - costs.min_commission) / (price * (1 + fee_rate)), costs.lot_size)
            value = shares * price
            fee = costs.commission(value)
        if shares <= 0 or value + fee > cash:
            continue
        cash -= value + fee
        units[code] = units.get(code, 0.0) + shares * raw / adj
        last_adj.setdefault(code, adj)
        trades.append(Trade(today, code, "buy", shares, price, value, fee, 0.0))
    return cash


def _round_lot(shares: float, lot: int) -> float:
    return math.floor(shares / lot + 1e-9) * lot
