"""基準策略（系統規格書 §4「基準比較」）。這些不是要拿來投資的策略，而是之後所有策略必須打敗的對照組。"""

from __future__ import annotations

import pandas as pd

from apex.backtest.engine import Context, Strategy


def buy_and_hold(code: str) -> Strategy:
    """全部資金買進單一標的並持有（例：0050）。"""
    def strategy(ctx: Context) -> dict[str, float]:
        return {code: 1.0}
    return strategy


def liquid_universe(ctx: Context, min_value: float = 50e6, min_price: float = 10.0, lookback: int = 20,
                    min_days: int = 60) -> pd.Index:
    """可投資的普通股：近 lookback 日平均成交金額 ≥ min_value、收盤價 ≥ min_price、至少交易 min_days 天。

    價格門檻用未還原價格；只使用訊號日（含）以前的資料。
    """
    sec = ctx.data.securities
    stocks = sec.index[sec["sec_type"] == "stock"]
    value = ctx.history("value", lookback)
    close = ctx.history("close", 1).iloc[-1]
    seen = ctx.history("close", min_days).notna().sum()
    cols = value.columns.intersection(stocks)
    ok = (value[cols].mean() >= min_value) & (close[cols] >= min_price) & (seen[cols] >= min_days)
    return ok.index[ok.fillna(False)]


def equal_weight_liquid(n: int = 50) -> Strategy:
    """成交金額最大的 n 檔普通股等權重：代表「不選股、只分散」的基準。"""
    def strategy(ctx: Context) -> dict[str, float]:
        universe = liquid_universe(ctx)
        value = ctx.history("value", 60)[universe].mean().nlargest(n)
        return {c: 1 / len(value) for c in value.index} if len(value) else {}
    return strategy


def momentum(n: int = 20, lookback: int = 240, skip: int = 20) -> Strategy:
    """過去 lookback 日（排除最近 skip 日）還原報酬最高的 n 檔，等權重：簡單量化基準。"""
    def strategy(ctx: Context) -> dict[str, float]:
        universe = liquid_universe(ctx)
        px = ctx.history("adj_close", lookback + 1)[universe]
        if len(px) < lookback + 1:
            return {}
        ret = (px.iloc[-1 - skip] / px.iloc[0] - 1).dropna()
        top = ret.nlargest(n)
        return {c: 1 / len(top) for c in top.index} if len(top) else {}
    return strategy


STRATEGIES = {
    "buyhold": lambda arg: buy_and_hold(arg or "0050"),
    "equal": lambda arg: equal_weight_liquid(int(arg or 50)),
    "momentum": lambda arg: momentum(int(arg or 20)),
}


def from_spec(spec: str) -> Strategy:
    """'buyhold:0050'、'equal:50'、'momentum:20'。"""
    name, _, arg = spec.partition(":")
    if name not in STRATEGIES:
        raise ValueError(f"未知策略 {name}，可用：{', '.join(STRATEGIES)}")
    return STRATEGIES[name](arg)
