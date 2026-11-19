"""個人持股與交易紀錄（只存本機，見 docs/技術選型.md §3、docs/持股與每日報告.md）。

交易紀錄是 `personal/trades.csv`（.gitignore 已排除 personal/），可以用指令新增，也可以用 Excel 直接編輯：

    date,code,side,shares,price,fee,tax,note
    2026-11-18,0050,buy,100,195.5,27,0,定期定額

計價方式：
- 持股價值用「還原股價」計算總報酬：除息的現金股利視為再投入，除權配股、分割自動換算成約當股數。
  因此「約當股數」可能和券商顯示的股數不同，但總報酬與帳面損益一致。
- 平均成本法：買進成本含手續費；賣出時依賣出比例扣除成本，計算已實現損益（扣手續費與證交稅）。
- 帳戶報酬用時間加權報酬（排除買進、賣出的資金進出），用來計算回撤。
"""

from __future__ import annotations

import csv
import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from apex.backtest.costs import CostModel

COLUMNS = ["date", "code", "side", "shares", "price", "fee", "tax", "note"]

# 回撤提醒（使用者 2026-11-19 決定：目標 −25%，必要時可到 −30%）
DRAWDOWN_LEVELS = [(-0.30, "已超過可承受上限 −30%"), (-0.25, "已達回撤目標 −25%"),
                   (-0.20, "接近回撤目標 −25%"), (-0.10, "回撤超過 −10%")]
MAX_SINGLE_STOCK_WEIGHT = 0.30  # 單一個股（不含 ETF）占持股比例上限提醒


def broker_fee(value: float, costs: CostModel) -> int:
    """券商手續費：成交金額 × 0.1425% × 折扣，無條件捨去，最低 min_commission。"""
    if value <= 0:
        return 0
    fee = math.floor(value * costs.commission_rate * costs.commission_discount)
    return int(max(costs.min_commission, fee))


def sell_tax(value: float, sec_type: str, code: str, costs: CostModel) -> int:
    """證交稅（賣出）：股票 0.3%、ETF 0.1%、債券 ETF 0%，無條件捨去。"""
    return int(math.floor(costs.tax(value, sec_type, code)))


# ── 交易紀錄檔 ────────────────────────────────────────────────────────────

class TradeError(ValueError):
    pass


def load_trades(path: Path) -> pd.DataFrame:
    """讀取交易紀錄並檢查格式；檔案不存在時回傳空表。"""
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, dtype={"code": str, "note": str}, keep_default_na=False)
    missing = [c for c in COLUMNS[:5] if c not in df.columns]
    if missing:
        raise TradeError(f"{path} 缺少欄位：{', '.join(missing)}")
    for c in ("fee", "tax"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0) if c in df.columns else 0.0
    if "note" not in df.columns:
        df["note"] = ""
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["side"] = df["side"].str.strip().str.lower()
    df["code"] = df["code"].str.strip()
    bad = df[~df["side"].isin(["buy", "sell"])]
    if len(bad):
        raise TradeError(f"side 只能是 buy 或 sell：第 {', '.join(str(i + 2) for i in bad.index)} 行")
    if (df["shares"] <= 0).any() or (df["price"] <= 0).any():
        raise TradeError("股數與價格必須大於 0")
    return df[COLUMNS].sort_values(["date", "side"], kind="stable").reset_index(drop=True)


def append_trade(path: Path, date: dt.date, code: str, side: str, shares: float, price: float,
                 fee: float, tax: float, note: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(COLUMNS)
        w.writerow([date.isoformat(), code, side, f"{shares:g}", f"{price:g}", f"{fee:g}", f"{tax:g}", note])


# ── 計算 ──────────────────────────────────────────────────────────────────

@dataclass
class Holding:
    code: str
    name: str
    sec_type: str
    shares: float  # 約當股數（含配股、分割、股利再投入）
    cost: float  # 剩餘成本（含買進手續費）
    value: float  # 最新市值（收盤價）
    close: float
    weight: float = 0.0
    drawdown_60: float = float("nan")  # 相對近 60 日最高點（還原）的跌幅
    ma_gap_60: float = float("nan")  # 相對 60 日均線

    @property
    def pnl(self) -> float:
        return self.value - self.cost

    @property
    def pnl_pct(self) -> float:
        return self.pnl / self.cost if self.cost else float("nan")


@dataclass
class PortfolioReport:
    as_of: pd.Timestamp
    holdings: list[Holding]
    realized: float
    invested: float  # 累計買進金額（含手續費）
    index: pd.Series  # 時間加權報酬指數（起點 1）
    warnings: list[str] = field(default_factory=list)

    @property
    def value(self) -> float:
        return sum(h.value for h in self.holdings)

    @property
    def cost(self) -> float:
        return sum(h.cost for h in self.holdings)

    @property
    def drawdown(self) -> float:
        if self.index.empty:
            return 0.0
        return float(self.index.iloc[-1] / self.index.cummax().iloc[-1] - 1)

    @property
    def max_drawdown(self) -> float:
        if self.index.empty:
            return 0.0
        return float((self.index / self.index.cummax() - 1).min())


def compute(trades: pd.DataFrame, close: pd.DataFrame, adj_close: pd.DataFrame, securities: pd.DataFrame,
            as_of: pd.Timestamp | None = None) -> PortfolioReport:
    """依交易紀錄與日行情（交易日 × 代號，未還原收盤與還原收盤）計算持股、損益與時間加權報酬。

    交易日必須是資料中的交易日；非交易日的交易視為下一個交易日。
    """
    days = close.index
    as_of = as_of or days[-1]
    days = days[days <= as_of]
    trades = trades[trades["date"] <= as_of].copy()
    if trades.empty:
        return PortfolioReport(as_of, [], 0.0, 0.0, pd.Series(dtype=float))
    codes = sorted(set(trades["code"]))
    missing = [c for c in codes if c not in close.columns]
    if missing:
        raise TradeError(f"找不到行情：{', '.join(missing)}")
    px = adj_close[codes].reindex(days).ffill()
    raw = close[codes].reindex(days).ffill()
    factor = px / raw  # 還原因子：還原價 ÷ 原始價
    pos = days.searchsorted(trades["date"])
    if (pos >= len(days)).any():
        raise TradeError("有交易日期晚於最新行情日")
    trades["day"] = days[pos]

    units = dict.fromkeys(codes, 0.0)  # 還原單位：價值 = 單位 × 還原收盤
    cost = dict.fromkeys(codes, 0.0)
    realized = invested = 0.0
    flows = pd.Series(0.0, index=days)
    by_day = {d: g for d, g in trades.groupby("day")}
    values = []
    start = trades["day"].min()
    for d in days[days >= start]:
        for _, t in by_day.get(d, pd.DataFrame()).iterrows():
            c, sh, price = t["code"], float(t["shares"]), float(t["price"])
            # 價值 = 股數 × 原始收盤 = (股數 ÷ 因子) × 還原收盤 → 還原單位 = 股數 ÷ 因子
            u = sh / factor.at[d, c]
            if t["side"] == "buy":
                paid = sh * price + t["fee"]
                units[c] += u
                cost[c] += paid
                invested += paid
                flows[d] += paid
            else:
                if u > units[c] * (1 + 1e-6):
                    raise TradeError(f"{d.date()} 賣出 {c} {sh:g} 股，超過持有約當股數 "
                                     f"{units[c] * factor.at[d, c]:.0f}")
                received = sh * price - t["fee"] - t["tax"]
                frac = min(1.0, u / units[c]) if units[c] else 1.0
                removed = cost[c] * frac
                realized += received - removed
                cost[c] -= removed
                units[c] = max(0.0, units[c] - u)
                flows[d] -= received
        values.append(sum(units[c] * px.at[d, c] for c in codes
                          if units[c] > 0 and not np.isnan(px.at[d, c])))
    v = pd.Series(values, index=days[days >= start])
    f = flows.reindex(v.index)
    prev = v.shift(1).fillna(0.0)
    denom = prev + f.clip(lower=0)
    ret = ((v - prev - f) / denom.where(denom > 0)).fillna(0.0)
    index = (1 + ret).cumprod()

    sec = securities
    holdings = []
    for c in codes:
        if units[c] <= 1e-9:
            continue
        val = units[c] * px.at[as_of, c]
        hist = adj_close[c].reindex(days).ffill()
        hi60 = hist.iloc[-60:].max()
        ma60 = hist.iloc[-60:].mean()
        holdings.append(Holding(
            code=c, name=str(sec.at[c, "name"]) if c in sec.index else "",
            sec_type=str(sec.at[c, "sec_type"]) if c in sec.index else "",
            shares=units[c] * factor.at[as_of, c], cost=cost[c], value=val, close=float(raw.at[as_of, c]),
            drawdown_60=float(hist.iloc[-1] / hi60 - 1), ma_gap_60=float(hist.iloc[-1] / ma60 - 1)))
    total = sum(h.value for h in holdings)
    for h in holdings:
        h.weight = h.value / total if total else 0.0
    report = PortfolioReport(as_of, sorted(holdings, key=lambda h: -h.value), realized, invested, index)
    report.warnings = warnings(report, close, securities)
    return report


def warnings(r: PortfolioReport, close: pd.DataFrame, securities: pd.DataFrame) -> list[str]:
    out = []
    dd = r.drawdown
    for level, msg in DRAWDOWN_LEVELS:
        if dd <= level:
            out.append(f"帳戶{msg}：目前比最高點低 {dd:.1%}")
            break
    for h in r.holdings:
        if h.sec_type != "etf" and h.weight > MAX_SINGLE_STOCK_WEIGHT:
            out.append(f"{h.code} {h.name} 占持股 {h.weight:.0%}，單一個股超過 {MAX_SINGLE_STOCK_WEIGHT:.0%}")
        last = close[h.code].loc[:r.as_of].dropna()
        if len(last) and last.index[-1] < r.as_of:
            out.append(f"{h.code} {h.name} 最後成交日 {last.index[-1].date()}，可能停止交易或下市")
        if h.drawdown_60 <= -0.20:
            out.append(f"{h.code} {h.name} 比近 60 日高點低 {h.drawdown_60:.0%}")
    return out


def load_prices(db, codes: list[str], start: dt.date, end: dt.date | None = None) -> tuple[pd.DataFrame, ...]:
    """從資料庫取持股相關代號的日行情（只含 ok 交易日）。"""
    end = end or dt.date.today()
    df = db.con.execute(f"""
        SELECT date, code, close, adj_close FROM daily_quotes_adj
        WHERE code IN ({",".join("?" * len(codes))}) AND date BETWEEN ? AND ?
          AND date IN (SELECT date FROM ingest_status GROUP BY date HAVING bool_and(status = 'ok'))
    """, [*codes, start, end]).df()
    df["date"] = pd.to_datetime(df["date"])
    close = df.pivot(index="date", columns="code", values="close").sort_index()
    adj = df.pivot(index="date", columns="code", values="adj_close").reindex(close.index)
    return close, adj


def text(r: PortfolioReport) -> str:
    if not r.holdings and r.invested == 0:
        return "尚無交易紀錄（personal/trades.csv）。可用 `apex portfolio add` 新增。"
    lines = [f"持股報告｜資料日 {r.as_of.date()}", "",
             "| 代號 | 名稱 | 約當股數 | 收盤 | 市值 | 成本 | 損益 | 報酬 | 比重 | 距60日高點 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for h in r.holdings:
        lines.append(f"| {h.code} | {h.name} | {h.shares:,.0f} | {h.close:,.2f} | {h.value:,.0f} | "
                     f"{h.cost:,.0f} | {h.pnl:+,.0f} | {h.pnl_pct:+.1%} | {h.weight:.0%} | "
                     f"{h.drawdown_60:+.1%} |")
    unreal = r.value - r.cost
    lines += ["",
              f"總市值 {r.value:,.0f}｜持股成本 {r.cost:,.0f}｜未實現損益 {unreal:+,.0f}｜"
              f"已實現損益 {r.realized:+,.0f}",
              f"帳戶時間加權報酬 {r.index.iloc[-1] - 1:+.1%}｜目前回撤 {r.drawdown:+.1%}｜"
              f"最大回撤 {r.max_drawdown:+.1%}（目標 −25%、上限 −30%）"]
    if r.warnings:
        lines += ["", "提醒："] + [f"  ! {w}" for w in r.warnings]
    else:
        lines += ["", "提醒：無"]
    return "\n".join(lines)
