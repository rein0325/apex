"""籌碼：三大法人買賣超、融資融券（上市＋上櫃，每日）。

三大法人單位為「股」；融資融券單位為「張」（交易單位）。
外資一律為「外資及陸資合計」（含外資自營商）：證交所 2017-12-18 起才分開列示外資自營商，
合計後新舊格式一致；分開的外資自營商另存 foreign_dealer_net（舊格式為 None）。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from apex.parsing import classify, to_int
from apex.sources import NotPublished, ParseError

TWSE_INSTI_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
TPEX_INSTI_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
TPEX_MARGIN_URL = "https://www.tpex.org.tw/www/zh-tw/margin/balance"


def twse_insti_params(date: dt.date) -> dict[str, str]:
    return {"date": date.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"}


def twse_margin_params(date: dt.date) -> dict[str, str]:
    return {"date": date.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"}


def tpex_insti_params(date: dt.date) -> dict[str, str]:
    return {"type": "Daily", "sect": "EW", "date": date.strftime("%Y/%m/%d"), "response": "json"}


def tpex_margin_params(date: dt.date) -> dict[str, str]:
    return {"date": date.strftime("%Y/%m/%d"), "response": "json"}


@dataclass(frozen=True)
class Insti:
    date: dt.date
    market: str
    code: str
    foreign_buy: int | None
    foreign_sell: int | None
    foreign_net: int | None  # 外資及陸資合計（含外資自營商）
    foreign_dealer_net: int | None  # 外資自營商（2017-12-18 前證交所未分開，為 None）
    trust_buy: int | None
    trust_sell: int | None
    trust_net: int | None
    dealer_net: int | None  # 自營商合計
    dealer_self_net: int | None  # 自營商（自行買賣）
    dealer_hedge_net: int | None  # 自營商（避險）
    total_net: int | None


@dataclass(frozen=True)
class Margin:
    date: dt.date
    market: str
    code: str
    margin_buy: int | None
    margin_sell: int | None
    margin_repay: int | None  # 現金償還
    margin_balance: int | None
    margin_limit: int | None
    short_sell: int | None
    short_buy: int | None  # 融券買進（回補）
    short_repay: int | None  # 現券償還
    short_balance: int | None
    short_limit: int | None
    offset: int | None  # 資券互抵（當日沖銷）


def _load(text: str, label: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"{label} 不是 JSON：{text[:100]!r}") from e


def _rows(rows: list[list], date: dt.date) -> list[tuple[str, list]]:
    """略過非證券列與權證，回傳 (代號, 列)。"""
    out = []
    for row in rows:
        code = str(row[0]).strip()
        if not code.isascii() or not code.isalnum() or classify(code) == "warrant":
            continue
        out.append((code, row))
    return out


def _sum(*values: int | None) -> int | None:
    return None if any(v is None for v in values) else sum(values)  # type: ignore[arg-type]


# --- 三大法人 ---------------------------------------------------------------

TWSE_INSTI_NEW = [
    "證券代號", "證券名稱", "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)",
    "外陸資買賣超股數(不含外資自營商)", "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
    "投信買進股數", "投信賣出股數", "投信買賣超股數", "自營商買賣超股數", "自營商買進股數(自行買賣)",
    "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)", "自營商買進股數(避險)", "自營商賣出股數(避險)",
    "自營商買賣超股數(避險)", "三大法人買賣超股數",
]
TWSE_INSTI_OLD = [
    "證券代號", "證券名稱", "外資買進股數", "外資賣出股數", "外資買賣超股數", "投信買進股數", "投信賣出股數",
    "投信買賣超股數", "自營商買賣超股數", "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)",
    "自營商買賣超股數(自行買賣)", "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)",
    "三大法人買賣超股數",
]


def parse_twse_insti(text: str, date: dt.date) -> list[Insti]:
    data = _load(text, "證交所三大法人")
    if data.get("stat") != "OK":
        raise NotPublished(f"證交所三大法人 {date}：{data.get('stat')}")
    if data.get("date") != date.strftime("%Y%m%d"):
        raise ParseError(f"證交所三大法人回應日期 {data.get('date')} 與要求 {date} 不符")
    fields = [f.strip() for f in data.get("fields") or []]
    out = []
    if fields == TWSE_INSTI_NEW:
        for code, r in _rows(data.get("data") or [], date):
            v = [to_int(x) for x in r]
            out.append(Insti(date, "TWSE", code, _sum(v[2], v[5]), _sum(v[3], v[6]), _sum(v[4], v[7]), v[7],
                             v[8], v[9], v[10], v[11], v[14], v[17], v[18]))
    elif fields == TWSE_INSTI_OLD:
        for code, r in _rows(data.get("data") or [], date):
            v = [to_int(x) for x in r]
            out.append(Insti(date, "TWSE", code, v[2], v[3], v[4], None, v[5], v[6], v[7], v[8], v[11], v[14],
                             v[15]))
    else:
        raise ParseError(f"證交所三大法人欄位不符：{fields}")
    return out


TPEX_INSTI_OLD = [
    "代號", "名稱", "外資及陸資買股數", "外資及陸資賣股數", "外資及陸資淨買股數", "投信買進股數",
    "投信賣股數", "投信淨買股數", "自營淨買股數", "自營商(自行買賣)買股數", "自營商(自行買賣)賣股數",
    "自營商(自行買賣)淨買股數", "自營商(避險)買股數", "自營商(避險)賣股數", "自營商(避險)淨買股數",
    "三大法人買賣超股數",
]


def parse_tpex_insti(text: str, date: dt.date) -> list[Insti]:
    """新格式（24 欄）欄名重複，依網頁表頭的分組順序：外資(不含外資自營商)、外資自營商、外資及陸資、
    投信、自營商(自行買賣)、自營商(避險)、自營商，各為買進／賣出／買賣超，最後為合計。"""
    data = _load(text, "櫃買三大法人")
    if str(data.get("stat", "")).lower() != "ok":
        raise NotPublished(f"櫃買三大法人 {date}：{data.get('stat')}")
    if data.get("date") != date.strftime("%Y%m%d"):
        raise ParseError(f"櫃買三大法人回應日期 {data.get('date')} 與要求 {date} 不符")
    table = next((t for t in data.get("tables") or [] if t.get("data")), None)
    if table is None:
        raise NotPublished(f"櫃買三大法人 {date}：沒有資料")
    fields = [f.strip() for f in table.get("fields") or []]
    out = []
    if len(fields) == 24 and fields[0] == "代號" and fields[-1] == "三大法人買賣超股數合計":
        for code, r in _rows(table["data"], date):
            v = [to_int(x) for x in r]
            out.append(Insti(date, "TPEX", code, v[8], v[9], v[10], v[7], v[11], v[12], v[13], v[22], v[16],
                             v[19], v[23]))
    elif fields == TPEX_INSTI_OLD:
        for code, r in _rows(table["data"], date):
            v = [to_int(x) for x in r]
            out.append(Insti(date, "TPEX", code, v[2], v[3], v[4], None, v[5], v[6], v[7], v[8], v[11], v[14],
                             v[15]))
    else:
        raise ParseError(f"櫃買三大法人欄位不符：{fields}")
    return out


# --- 融資融券 ---------------------------------------------------------------

TWSE_MARGIN = ["代號", "名稱", "買進", "賣出", "現金償還", "前日餘額", "今日餘額", "次一營業日限額",
               "買進", "賣出", "現券償還", "前日餘額", "今日餘額", "次一營業日限額", "資券互抵", "註記"]
TPEX_MARGIN = ["代號", "名稱", "前資餘額(張)", "資買", "資賣", "現償", "資餘額", "資屬證金", "資使用率(%)",
               "資限額", "前券餘額(張)", "券賣", "券買", "券償", "券餘額", "券屬證金", "券使用率(%)",
               "券限額", "資券相抵(張)", "備註"]


def parse_twse_margin(text: str, date: dt.date) -> list[Margin]:
    data = _load(text, "證交所融資融券")
    if data.get("stat") != "OK":
        raise NotPublished(f"證交所融資融券 {date}：{data.get('stat')}")
    if data.get("date") != date.strftime("%Y%m%d"):
        raise ParseError(f"證交所融資融券回應日期 {data.get('date')} 與要求 {date} 不符")
    table = next((t for t in data.get("tables") or [] if "融資融券彙總" in str(t.get("title"))), None)
    if table is None or not table.get("data"):
        raise NotPublished(f"證交所融資融券 {date}：沒有個股資料")
    if [f.strip() for f in table.get("fields") or []] != TWSE_MARGIN:
        raise ParseError(f"證交所融資融券欄位不符：{table.get('fields')}")
    out = []
    for code, r in _rows(table["data"], date):
        v = [to_int(x) for x in r[:15]]
        out.append(Margin(date, "TWSE", code, v[2], v[3], v[4], v[6], v[7], v[9], v[8], v[10], v[12], v[13],
                          v[14]))
    return out


def parse_tpex_margin(text: str, date: dt.date) -> list[Margin]:
    data = _load(text, "櫃買融資融券")
    if str(data.get("stat", "")).lower() != "ok":
        raise NotPublished(f"櫃買融資融券 {date}：{data.get('stat')}")
    if data.get("date") != date.strftime("%Y%m%d"):
        raise ParseError(f"櫃買融資融券回應日期 {data.get('date')} 與要求 {date} 不符")
    table = (data.get("tables") or [{}])[0]
    if not table.get("data"):
        raise NotPublished(f"櫃買融資融券 {date}：沒有資料")
    if [f.strip() for f in table.get("fields") or []] != TPEX_MARGIN:
        raise ParseError(f"櫃買融資融券欄位不符：{table.get('fields')}")
    out = []
    for code, r in _rows(table["data"], date):
        v = [to_int(x) for x in r[:19]]
        out.append(Margin(date, "TPEX", code, v[3], v[4], v[5], v[6], v[9], v[11], v[12], v[13], v[14], v[17],
                          v[18]))
    return out
