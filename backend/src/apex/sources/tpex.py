"""櫃買中心（上櫃）。"""

from __future__ import annotations

import datetime as dt
import json

from apex.parsing import classify, roc_date, to_float, to_int
from apex.sources import NotPublished, ParseError, Quote

QUOTES_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"

MAIN_TABLE = "上櫃股票行情"
MANAGED_TABLE = "管理股票"


def quotes_params(date: dt.date) -> dict[str, str]:
    return {"date": date.strftime("%Y/%m/%d"), "response": "json"}


def _price(text: str) -> float | None:
    """價格；2011 年等早期資料無成交時填 0.00，視為沒有價格。"""
    v = to_float(text)
    return v if v else None


def parse_quotes(text: str, date: dt.date) -> list[Quote]:
    """解析上櫃每日行情（含管理股票）。排除權證；無成交的價格為 None。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"不是 JSON：{text[:100]!r}") from e
    if str(data.get("stat", "")).lower() != "ok":
        raise NotPublished(f"TPEx {date}：{data.get('stat')}")
    if data.get("date") and roc_date(data["date"]) != date:
        raise ParseError(f"TPEx 回應日期 {data['date']} 與要求 {date} 不符")

    tables = {t.get("title"): t for t in data.get("tables", [])}
    if MAIN_TABLE not in tables:
        raise ParseError(f"TPEx {date}：找不到「{MAIN_TABLE}」表，實際：{list(tables)}")

    quotes = []
    for title, managed in ((MAIN_TABLE, False), (MANAGED_TABLE, True)):
        table = tables.get(title)
        if not table:
            continue
        fields = table.get("fields") or []
        try:
            idx = {name: fields.index(name) for name in
                   ("代號", "名稱", "收盤", "開盤", "最高", "最低", "成交股數", "成交金額(元)", "成交筆數")}
        except ValueError as e:
            raise ParseError(f"TPEx {date}「{title}」欄位不符：{fields}") from e
        for row in table.get("data", []):
            code = row[idx["代號"]].strip()
            if not code.isascii() or not code.isalnum():
                continue  # 2015-11-23 等日期混入表頭列
            sec_type = classify(code)
            if sec_type == "warrant":
                continue
            quotes.append(Quote(
                date=date, market="TPEX", code=code, name=row[idx["名稱"]].strip(), sec_type=sec_type,
                open=_price(row[idx["開盤"]]), high=_price(row[idx["最高"]]),
                low=_price(row[idx["最低"]]), close=_price(row[idx["收盤"]]),
                volume=to_int(row[idx["成交股數"]]), value=to_int(row[idx["成交金額(元)"]]),
                trades=to_int(row[idx["成交筆數"]]), is_managed=managed,
            ))
    if not quotes:
        # 櫃買中心在假日或尚未發布時回傳 stat=ok 但表格為空
        raise NotPublished(f"TPEx {date}：行情表為空")
    return quotes
