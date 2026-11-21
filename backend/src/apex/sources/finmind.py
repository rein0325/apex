"""FinMind 免費 API：補齊已下市櫃公司的月營收與財報。

公開資訊觀測站的彙總表會重新產生，只包含**目前**上市櫃的公司（2015-01 的頁面中，之後下市的 123 檔
普通股只剩 1 檔）。只用彙總表會造成倖存者偏差，所以已下市櫃的公司改由 FinMind 逐檔補齊。
FinMind 的損益為單季數字，轉成與觀測站相同的年初累計後寫入。
未登入的免費額度約每小時 300 次，請求間隔設為 13 秒。
"""

from __future__ import annotations

import json
from collections import defaultdict

from apex.sources import NotPublished, ParseError
from apex.sources.fundamentals import BALANCE_ITEMS, INCOME_ITEMS, FinRow, Revenue, revenue_available

API = "https://api.finmindtrade.com/api/v4/data"
DELAY_SEC = 13.0
DATASETS = {"revenue": "TaiwanStockMonthRevenue", "income": "TaiwanStockFinancialStatements",
            "balance": "TaiwanStockBalanceSheet"}


# 2026-11-22 回補早期資料：月營收從 2011、財報從 2013（IFRS，與觀測站一致）起
MIN_REVENUE_YEAR = 2011
MIN_FIN_YEAR = 2013


def params(dataset: str, code: str, start: str = "2015-01-01", end: str = "2030-12-31") -> dict[str, str]:
    if dataset == "info":  # 全部公司一次回傳，code 只是原始檔的鍵
        return {"dataset": "TaiwanStockInfo"}
    return {"dataset": DATASETS[dataset], "data_id": code, "start_date": start, "end_date": end}


# 標準化欄位 → FinMind type（依序取第一個有值的）
INCOME_MAP: dict[str, list[str]] = {
    "revenue": ["Revenue"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncome"],
    "pretax_income": ["PreTaxIncome", "IncomeBeforeTaxFromContinuingOperations"],
    "net_income": ["IncomeAfterTaxes", "IncomeAfterTax", "TotalConsolidatedProfitForThePeriod"],
    "net_income_parent": ["EquityAttributableToOwnersOfParent"],
    "eps": ["EPS"],
}
BALANCE_MAP: dict[str, list[str]] = {
    "current_assets": ["CurrentAssets"],
    "total_assets": ["TotalAssets"],
    "current_liabilities": ["CurrentLiabilities"],
    "total_liabilities": ["Liabilities"],
    "share_capital": ["CapitalStock", "OrdinaryShare"],
    "equity_parent": ["EquityAttributableToOwnersOfParent"],
    "equity": ["Equity"],
    "bvps": [],
}


def _data(text: str) -> list[dict]:
    try:
        d = json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"FinMind 不是 JSON：{text[:100]!r}") from e
    if d.get("status") != 200:
        msg = str(d.get("msg"))
        if "limit" in msg.lower() or "upper" in msg.lower():
            raise NotPublished(f"FinMind 額度用完：{msg}")
        raise ParseError(f"FinMind：{msg}")
    return d.get("data") or []


def parse_revenue(text: str, code: str, market: str, name: str) -> list[Revenue]:
    out = []
    for r in _data(text):
        y, m = int(r["revenue_year"]), int(r["revenue_month"])
        if y < MIN_REVENUE_YEAR:
            continue
        out.append(Revenue(y, m, market, code, name, None, int(r["revenue"]), None, None, None, None,
                           revenue_available(y, m)))
    return out


def _by_quarter(rows: list[dict]) -> dict[tuple[int, int], dict[str, float]]:
    out: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
    for r in rows:
        y, mo = int(r["date"][:4]), int(r["date"][5:7])
        if mo % 3 or y < MIN_FIN_YEAR:
            continue
        out[(y, mo // 3)][r["type"]] = float(r["value"])
    return out


def _pick(values: dict[str, float], names: list[str]) -> float | None:
    return next((values[n] for n in names if n in values), None)


def parse_income(text: str, code: str, name: str) -> dict[tuple[int, int], FinRow]:
    """單季數字 → 年初累計（與觀測站一致）。同年前一季缺漏時，之後各季累計數為 None。"""
    quarters = _by_quarter(_data(text))
    out = {}
    for (y, q) in sorted(quarters):
        v = quarters[(y, q)]
        single = {k: _pick(v, names) for k, names in INCOME_MAP.items()}
        if single["revenue"] is None and "NetInterestIncome" in v:  # 銀行：淨收益
            single["revenue"] = v["NetInterestIncome"] + v.get("NetNonInterestIncome", 0.0)
        layout = "financial" if "NetInterestIncome" in v or "Revenue" not in v else "general"
        if q == 1:
            ytd = single
        else:
            prev = out.get((y, q - 1))
            ytd = {k: (None if prev is None or prev.values[k] is None or single[k] is None
                       else prev.values[k] + single[k]) for k in INCOME_ITEMS}
        out[(y, q)] = FinRow("income", code, name, layout, ytd)
    return out


INFO_DATASET = "TaiwanStockInfo"
_INDUSTRY_ALIASES = {"觀光事業": "觀光餐旅", "其他": "其他業"}


def parse_industries(text: str, official: set[str]) -> dict[str, str]:
    """TaiwanStockInfo（含已下市公司）→ {代號: 產業別}，產業名稱對應到證交所一覽表的名稱。

    同一代號可能有多筆（含「電子工業」等舊分類、ETF 類別）：只保留能對應到正式產業名稱者，
    優先採用「其他業」以外、日期較新的一筆。
    """
    best: dict[str, tuple[bool, str, str]] = {}
    for r in _data(text):
        name = str(r.get("industry_category", "")).strip().removesuffix("類")
        name = _INDUSTRY_ALIASES.get(name, name)
        if name not in official and name + "業" in official:
            name += "業"
        if name not in official:
            continue
        rank = (name != "其他業", str(r.get("date", "")), name)
        code = str(r["stock_id"])
        if code not in best or rank > best[code]:
            best[code] = rank
    return {code: rank[2] for code, rank in best.items()}


def parse_balance(text: str, code: str, name: str) -> dict[tuple[int, int], FinRow]:
    quarters = _by_quarter(_data(text))
    out = {}
    for key, v in quarters.items():
        values = {k: _pick(v, BALANCE_MAP[k]) for k in BALANCE_ITEMS}
        layout = "general" if "CurrentAssets" in v else "financial"
        out[key] = FinRow("balance", code, name, layout, values)
    return out
