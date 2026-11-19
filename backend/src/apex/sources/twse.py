"""證交所（上市）。"""

from __future__ import annotations

import datetime as dt
import json

from apex.parsing import classify, roc_date, to_float, to_int
from apex.sources import NotPublished, ParseError, Quote

QUOTES_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TAIEX_MONTH_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"


def quotes_params(date: dt.date) -> dict[str, str]:
    return {"date": date.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"}


def taiex_month_params(year: int, month: int) -> dict[str, str]:
    return {"date": f"{year:04d}{month:02d}01", "response": "json"}


def _load(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"不是 JSON：{text[:100]!r}") from e


def parse_quotes(text: str, date: dt.date) -> list[Quote]:
    """解析每日收盤行情。排除權證；無成交的價格為 None。"""
    data = _load(text)
    if data.get("stat") != "OK":
        raise NotPublished(f"TWSE {date}：{data.get('stat')}")
    if data.get("date") and roc_date(data["date"]) != date:
        raise ParseError(f"TWSE 回應日期 {data['date']} 與要求 {date} 不符")

    for table in data.get("tables", []):
        fields = table.get("fields") or []
        if "證券代號" in fields and "收盤價" in fields:
            idx = {name: fields.index(name) for name in fields}
            break
    else:
        raise ParseError(f"TWSE {date}：找不到每日收盤行情表")

    quotes = []
    for row in table.get("data", []):
        code = row[idx["證券代號"]].strip()
        if not code.isascii() or not code.isalnum():
            continue  # 非證券列（表頭、說明）
        sec_type = classify(code)
        if sec_type == "warrant":
            continue
        quotes.append(Quote(
            date=date, market="TWSE", code=code, name=row[idx["證券名稱"]].strip(), sec_type=sec_type,
            open=to_float(row[idx["開盤價"]]), high=to_float(row[idx["最高價"]]),
            low=to_float(row[idx["最低價"]]), close=to_float(row[idx["收盤價"]]),
            volume=to_int(row[idx["成交股數"]]), value=to_int(row[idx["成交金額"]]),
            trades=to_int(row[idx["成交筆數"]]),
        ))
    return quotes


TAIEX_TR_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MFI94U"


def parse_taiex_tr_month(text: str) -> list[tuple[dt.date, float | None]]:
    """發行量加權股價報酬指數（含息）月資料 → (日期, 收盤)。回測的市場基準應使用報酬指數。"""
    data = _load(text)
    if data.get("stat") != "OK":
        raise NotPublished(f"報酬指數月資料：{data.get('stat')}")
    fields = [f.replace("　", "") for f in data.get("fields") or []]
    if fields != ["日期", "發行量加權股價報酬指數"]:
        raise ParseError(f"報酬指數欄位不符：{fields}")
    return [(roc_date(r[0]), to_float(r[1])) for r in data.get("data") or []]


HOLIDAY_URL ="https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule"


def holiday_params(year: int) -> dict[str, str]:
    return {"date": f"{year:04d}0101", "response": "json"}


def parse_holidays(text: str, year: int) -> list[tuple[dt.date, str, bool]]:
    """市場開休市日期 → (日期, 名稱, 是否休市)。

    表中也列出「開始交易日」「最後交易日」等仍有交易的日子，名稱含「交易日」者視為開市。
    「市場無交易，僅辦理結算交割作業」為休市。該年度尚未公布時回傳 NotPublished。
    """
    data = _load(text)
    if str(data.get("stat", "")).lower() != "ok":
        raise NotPublished(f"{year} 休市日期：{data.get('stat')}")
    title = str(data.get("title", ""))
    if not title.startswith(f"{year - 1911} 年"):
        raise NotPublished(f"{year} 休市日期尚未公布（回應為 {title}）")
    if (data.get("fields") or [])[:2] != ["日期", "名稱"]:
        raise ParseError(f"休市日期欄位不符：{data.get('fields')}")
    out = []
    for row in data.get("data") or []:
        d = dt.date.fromisoformat(row[0])
        name = row[1].strip()
        out.append((d, name, "交易日" not in name))
    return out


def parse_taiex_month(text: str) -> list[dict]:
    """加權指數月資料 → 該月每個交易日的開高低收。用來建立交易日曆。"""
    data = _load(text)
    if data.get("stat") != "OK":
        raise NotPublished(f"TAIEX 月資料：{data.get('stat')}")
    fields = data.get("fields") or []
    if fields[:1] != ["日期"]:
        raise ParseError(f"TAIEX 月資料欄位不符：{fields}")
    return [
        {"date": roc_date(r[0]), "open": to_float(r[1]), "high": to_float(r[2]),
         "low": to_float(r[3]), "close": to_float(r[4])}
        for r in data.get("data", [])
    ]
