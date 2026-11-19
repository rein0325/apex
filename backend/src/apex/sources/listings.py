"""掛牌資訊：國際證券辨識號碼一覽表（產業別、上市櫃日）與終止上市櫃清單。

- 證交所 ISIN 一覽表（MS950 編碼的 HTML）：目前掛牌的全部有價證券，含產業別與上市（櫃）日。
  只有「目前」的資料，已下市櫃的證券查不到產業別。
- 證交所終止上市：一次回傳 2001 年至今全部。
- 櫃買中心終止上櫃：逐年查詢。上櫃轉上市也會列在這裡，所以終止上櫃不等於下市。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass

from apex.parsing import classify, roc_date
from apex.sources import ParseError

ISIN_URL = "https://isin.twse.com.tw/isin/C_public.jsp"
ISIN_ENCODING = "cp950"
ISIN_MODES = {"TWSE": "2", "TPEX": "4"}
TWSE_DELISTED_URL = "https://www.twse.com.tw/rwd/zh/company/suspendListing"
TPEX_DELISTED_URL = "https://www.tpex.org.tw/www/zh-tw/company/deListed"

# ISIN 一覽表的分段標題 → 本系統的證券類別（docs/資料層設計.md §3）
SECTION_TYPES = {
    "股票": "stock",
    "創新板": "stock",
    "ETF": "etf",
    "特別股": "preferred",
    "臺灣存託憑證(TDR)": "dr",
    "ETN": "etn",
    "受益證券-不動產投資信託": "reit",
}

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Listing:
    code: str
    name: str
    market: str  # 'TWSE' | 'TPEX'
    board: str  # 一覽表的市場別原文：上市、上市臺灣創新板、上櫃
    section: str  # 一覽表分段：股票、ETF、特別股…
    industry: str | None
    listed_date: dt.date | None
    isin: str
    cfi: str


@dataclass(frozen=True)
class Delisting:
    date: dt.date  # 終止上市（櫃）日期
    market: str
    code: str
    name: str
    reason: str | None = None


def parse_isin(text: str, market: str) -> list[Listing]:
    """解析 ISIN 一覽表，略過權證與其他本系統不收錄的分段。"""
    if "國際證券辨識號碼" not in text:
        raise ParseError(f"ISIN 一覽表格式不符：{text[:100]!r}")
    section = None
    rows = []
    for m in _ROW.finditer(text):
        cells = [_TAG.sub("", c).replace("&nbsp;", " ").strip() for c in _CELL.findall(m.group(1))]
        if len(cells) == 1:
            section = cells[0]
            continue
        if len(cells) != 7 or "　" not in cells[0] or section not in SECTION_TYPES:
            continue
        code, name = (s.strip() for s in cells[0].split("　", 1))
        if classify(code) == "warrant":
            continue
        rows.append(Listing(
            code=code, name=name, market=market, board=cells[3], section=section,
            industry=cells[4] or None,
            listed_date=dt.datetime.strptime(cells[2], "%Y/%m/%d").date() if cells[2] else None,
            isin=cells[1], cfi=cells[5],
        ))
    if not rows:
        raise ParseError("ISIN 一覽表沒有任何證券")
    return rows


def parse_twse_delisted(text: str) -> list[Delisting]:
    data = _json(text, "證交所終止上市")
    if str(data.get("status", data.get("stat", ""))).lower() != "ok":
        raise ParseError(f"證交所終止上市：{data.get('status')}")
    fields = data.get("fields") or []
    if fields != ["終止上市日期", "公司名稱", "上市編號"]:
        raise ParseError(f"證交所終止上市欄位不符：{fields}")
    return [Delisting(roc_date(d), "TWSE", str(code).strip(), name.strip())
            for d, name, code in data.get("data") or []]


def parse_tpex_delisted(text: str) -> list[Delisting]:
    data = _json(text, "櫃買中心終止上櫃")
    if str(data.get("stat", "")).lower() != "ok":
        raise ParseError(f"櫃買中心終止上櫃：{data.get('stat')}")
    table = (data.get("tables") or [{}])[0]
    fields = table.get("fields") or []
    cols = ["股票代號", "公司名稱", "終止上櫃日期", "終止上櫃原因"]
    if any(c not in fields for c in cols):
        raise ParseError(f"櫃買中心終止上櫃欄位不符：{fields}")
    i = {c: fields.index(c) for c in cols}
    return [Delisting(roc_date(str(r[i["終止上櫃日期"]])), "TPEX", str(r[i["股票代號"]]).strip(),
                      str(r[i["公司名稱"]]).strip(), str(r[i["終止上櫃原因"]]).strip() or None)
            for r in table.get("data") or []]


def _json(text: str, label: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"{label} 不是 JSON：{text[:100]!r}") from e
