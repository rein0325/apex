"""基本面：月營收、季財報（公開資訊觀測站，上市＋上櫃）。

可取得日（available_date）以法定公告期限的次日計算，回測只能使用 available_date ≤ 訊號日的資料：
- 月營收：次月 10 日前公告 → 次月 11 日。
- 財報（一般業）：Q1 5/15、Q2 8/14、Q3 11/14、年報 次年 3/31。
- 財報（金融、保險、證券等格式）：Q1 5/30、Q2 8/31、Q3 11/29、年報 次年 3/31（較晚，保守處理）。
實際上多數公司提早公告，用期限是保守做法，不會偷看到未來。

資料是公開資訊觀測站目前的版本：若公司事後更正，看到的是更正後數字（極少見）。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from apex.parsing import to_float
from apex.sources import NotPublished, ParseError

MOPS = "https://mopsov.twse.com.tw"
MOPS_ENCODING_REVENUE = "cp950"
MARKET_TYPEK = {"TWSE": "sii", "TPEX": "otc"}


def revenue_url(market: str, year: int, month: int, foreign: bool) -> str:
    """_0 為國內公司、_1 為外國公司（-KY）。"""
    return f"{MOPS}/nas/t21/{MARKET_TYPEK[market]}/t21sc03_{year - 1911}_{month}_{int(foreign)}.html"


FIN_URLS = {"income": f"{MOPS}/mops/web/ajax_t163sb04", "balance": f"{MOPS}/mops/web/ajax_t163sb05"}


def fin_form(market: str, year: int, quarter: int) -> dict[str, str]:
    return {"encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1", "isQuery": "Y",
            "TYPEK": MARKET_TYPEK[market], "year": str(year - 1911), "season": f"{quarter:02d}"}


_TAG = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return _TAG.sub("", s).replace("&nbsp;", "").strip()


# --- 月營收 -------------------------------------------------------------------

@dataclass(frozen=True)
class Revenue:
    year: int
    month: int
    market: str
    code: str
    name: str
    industry: str | None  # 公告當時的產業別（已下市公司的產業別也可由此取得）
    revenue: int | None  # 元
    revenue_last_year: int | None
    cum_revenue: int | None
    cum_revenue_last_year: int | None
    note: str | None
    available_date: dt.date


def revenue_available(year: int, month: int) -> dt.date:
    y, m = (year + 1, 1) if month == 12 else (year, month + 1)
    return dt.date(y, m, 11)


_REV_TOKEN = re.compile(r"產業別：([^<]*)<|<tr align=right>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)


def _thousand(v: str) -> int | None:
    f = to_float(v)
    return None if f is None else int(round(f * 1000))


def parse_revenue(text: str, market: str, year: int, month: int) -> list[Revenue]:
    """解析月營收彙總表（單位千元，轉成元）。該月尚未公告時回傳 NotPublished。"""
    title = re.search(r"(\d+)年(\d+)月份", text)
    if not title:
        if "查無" in text or "不存在" in text or "<html" not in text.lower()[:500]:
            raise NotPublished(f"{market} {year}-{month:02d} 月營收尚未公告")
        raise ParseError(f"月營收格式不符：{_clean(text)[:100]!r}")
    if (int(title.group(1)) + 1911, int(title.group(2))) != (year, month):
        raise ParseError(f"月營收頁面為 {title.group(0)}，與要求 {year}-{month:02d} 不符")
    industry, rows = None, []
    for m in _REV_TOKEN.finditer(text):
        if m.group(1) is not None:
            industry = m.group(1).strip() or None
            continue
        cells = [_clean(c) for c in _TD.findall(m.group(2))]
        if len(cells) != 11 or not re.fullmatch(r"[0-9A-Z]{4,6}", cells[0]):
            continue  # 合計列等
        rows.append(Revenue(
            year, month, market, cells[0], cells[1], industry, _thousand(cells[2]), _thousand(cells[4]),
            _thousand(cells[7]), _thousand(cells[8]), None if cells[10] in ("", "-") else cells[10],
            revenue_available(year, month)))
    return rows


# --- 季財報 -------------------------------------------------------------------

# 標準化欄位 → 各格式可能的欄名（依序取第一個出現的）。
FIN_ITEMS: dict[str, list[str]] = {
    "revenue": ["營業收入", "淨收益", "收益", "收入"],
    "gross_profit": ["營業毛利（毛損）淨額", "營業毛利（毛損）"],
    "operating_income": ["營業利益（損失）", "營業利益"],
    "pretax_income": ["稅前淨利（淨損）", "繼續營業單位稅前淨利（淨損）", "繼續營業單位稅前損益",
                      "繼續營業單位稅前純益（純損）"],
    "net_income": ["本期淨利（淨損）", "本期稅後淨利（淨損）"],
    "net_income_parent": ["淨利（淨損）歸屬於母公司業主", "淨利（損）歸屬於母公司業主"],
    "eps": ["基本每股盈餘（元）", "基本每股盈餘"],
    "current_assets": ["流動資產"],
    "total_assets": ["資產總計", "資產總額", "資產合計"],
    "current_liabilities": ["流動負債"],
    "total_liabilities": ["負債總計", "負債總額", "負債合計"],
    "share_capital": ["股本"],
    "equity_parent": ["歸屬於母公司業主之權益合計", "歸屬於母公司業主權益合計"],
    "equity": ["權益總計", "權益總額", "權益合計"],
    "bvps": ["每股參考淨值"],
}
INCOME_ITEMS = ["revenue", "gross_profit", "operating_income", "pretax_income", "net_income",
                "net_income_parent", "eps"]
BALANCE_ITEMS = [k for k in FIN_ITEMS if k not in INCOME_ITEMS]
# 每股數字以元為單位，其他金額為千元（轉成元）
PER_SHARE = {"eps", "bvps"}

DEADLINES_GENERAL = {1: (5, 15), 2: (8, 14), 3: (11, 14)}
DEADLINES_FINANCIAL = {1: (5, 30), 2: (8, 31), 3: (11, 29)}


def fin_available(year: int, quarter: int, financial: bool) -> dt.date:
    if quarter == 4:
        return dt.date(year + 1, 4, 1)
    m, d = (DEADLINES_FINANCIAL if financial else DEADLINES_GENERAL)[quarter]
    return dt.date(year, m, d) + dt.timedelta(days=1)


@dataclass(frozen=True)
class FinRow:
    statement: str  # 'income' | 'balance'
    code: str
    name: str
    layout: str  # 'general' | 'financial'：一般業或金融保險等格式（決定公告期限）
    values: dict[str, float | None]


_TABLE = re.compile(r"<table[^>]*class=.?hasBorder.*?</table>", re.S | re.I)
_TR = re.compile(r"<tr.*?</tr>", re.S | re.I)
_CELL = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)


def parse_financials(text: str, statement: str) -> list[FinRow]:
    """解析綜合損益表（年初至今累計）或資產負債表彙總。一頁內依產業格式分成多個表。"""
    tables = _TABLE.findall(text)
    if not tables:
        plain = _clean(text)
        if "查無" in plain or "無應編製" in plain:
            raise NotPublished(f"財報尚未公告：{plain[:60]!r}")
        if "頻繁" in plain or "稍後" in plain:
            raise NotPublished(f"查詢過於頻繁：{plain[:60]!r}")
        raise ParseError(f"財報格式不符：{re.sub(r'\s+', ' ', plain)[:100]!r}")
    items = INCOME_ITEMS if statement == "income" else BALANCE_ITEMS
    out = []
    for table in tables:
        trs = _TR.findall(table)
        header = [_clean(c) for c in _CELL.findall(trs[0])]
        if header[:2] != ["公司代號", "公司名稱"]:
            raise ParseError(f"財報表頭不符：{header[:5]}")
        col = {}
        for item in items:
            col[item] = next((header.index(n) for n in FIN_ITEMS[item] if n in header), None)
        # 有「營業收入」或「流動資產」的是一般業格式；其餘為金融、保險、證券等
        layout = "general" if ("營業收入" in header or "流動資產" in header) else "financial"
        for tr in trs[1:]:
            cells = [_clean(c) for c in _CELL.findall(tr)]
            if len(cells) != len(header) or not cells[0]:
                continue
            values = {}
            for item, i in col.items():
                v = to_float(cells[i]) if i is not None else None
                values[item] = v if v is None or item in PER_SHARE else v * 1000
            out.append(FinRow(statement, cells[0], cells[1], layout, values))
    return out
