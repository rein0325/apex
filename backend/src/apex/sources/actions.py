"""公司行動：除權息、減資、變更面額、ETF 分割／反分割（上市＋上櫃）。

各來源格式相同：事件日（除權息日或恢復買賣日）、代號、事件前收盤價、官方參考價。
還原因子＝參考價 ÷ 事件前收盤價，各類事件一致處理（見 docs/資料層設計.md §9）。

除權息採用「減除股利參考價」，而非「除權息參考價」：後者含現金增資認股權的理論價值，
交易所在現增除權日放寬漲跌幅：漲停以前者、跌停以後者計算（例：6225 2026-08-18 除權息參考價
30.04，漲停 48.80＝44.40×1.1、跌停 27.05＝30.04×0.9），價格可落在兩者之間。
現增需另外繳款認購，回測假設不認購，所以還原只扣股利；理論參考價另存 theo_ref 供品質檢查使用。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from apex.parsing import roc_date, to_float
from apex.sources import ParseError

TWSE = "https://www.twse.com.tw/rwd/zh"
TPEX = "https://www.tpex.org.tw/www/zh-tw"


@dataclass(frozen=True)
class Action:
    date: dt.date  # 公告的事件日；遇休市時實際生效日為之後第一個交易日
    market: str
    code: str
    name: str
    kind: str  # 'dividend' | 'reduction' | 'par_change' | 'split'
    prev_close: float | None
    ref_price: float | None  # 還原用參考價（除權息為減除股利參考價）
    theo_ref: float | None = None  # 含現金增資權值的理論參考價（與 ref_price 相同時為 None）
    note: str | None = None

    @property
    def factor(self) -> float | None:
        if not self.prev_close or not self.ref_price:
            return None
        return self.ref_price / self.prev_close


@dataclass(frozen=True)
class Spec:
    source: str
    market: str
    kind: str
    url: str
    date_fmt: str  # 查詢參數日期格式
    date_col: str
    code_col: str
    name_col: str
    prev_col: str
    ref_col: str
    note_col: str | None = None
    theo_col: str | None = None  # 含現增權值的參考價：決定跌停價，但不用於還原

    def params(self, start: dt.date, end: dt.date) -> dict[str, str]:
        return {"startDate": start.strftime(self.date_fmt), "endDate": end.strftime(self.date_fmt),
                "response": "json"}


SPECS = [
    Spec("twse_exright", "TWSE", "dividend", f"{TWSE}/exRight/TWT49U", "%Y%m%d",
         "資料日期", "股票代號", "股票名稱", "除權息前收盤價", "減除股利參考價", "權/息", "除權息參考價"),
    Spec("twse_reduction", "TWSE", "reduction", f"{TWSE}/reducation/TWTAUU", "%Y%m%d",
         "恢復買賣日期", "股票代號", "名稱", "停止買賣前收盤價格", "恢復買賣參考價", "減資原因",
         "除權參考價"),
    Spec("twse_parvalue", "TWSE", "par_change", f"{TWSE}/change/TWTB8U", "%Y%m%d",
         "恢復買賣日期", "股票代號", "名稱", "停止買賣前收盤價格", "恢復買賣參考價"),
    Spec("tpex_exright", "TPEX", "dividend", f"{TPEX}/bulletin/exDailyQ", "%Y/%m/%d",
         "除權息日期", "代號", "名稱", "除權息前收盤價", "減除股利參考價", "權/息", "除權息參考價"),
    Spec("tpex_reduction", "TPEX", "reduction", f"{TPEX}/bulletin/revivt", "%Y/%m/%d",
         "恢復買賣日期", "股票代號", "名稱", "最後交易日之收盤價格", "減資恢復買賣開始日參考價格",
         "減資原因", "除權參考價"),
    Spec("tpex_parvalue", "TPEX", "par_change", f"{TPEX}/bulletin/pvChgRslt", "%Y/%m/%d",
         "恢復買賣日期", "證券代號", "證券名稱", "最後交易日之收盤價格", "恢復買賣開始參考價"),
    # ETF 分割／反分割（0050 於 2025-06-18 一拆四等）。上櫃目前沒有案例，仍納入以涵蓋未來事件。
    Spec("twse_etfsplit", "TWSE", "split", f"{TWSE}/split/TWTCAU", "%Y%m%d",
         "恢復買賣日期", "ETF代號", "名稱", "停止買賣前收盤價格", "恢復買賣參考價", "分割(反分割)"),
    Spec("tpex_etfsplit", "TPEX", "split", f"{TPEX}/bulletin/etfSplitRslt", "%Y/%m/%d",
         "恢復買賣日期", "證券代號", "證券名稱", "最後交易日之收盤價格", "恢復買賣開始參考價"),
    Spec("tpex_etfrevsplit", "TPEX", "split", f"{TPEX}/bulletin/etfRvsRslt", "%Y/%m/%d",
         "恢復買賣日期", "證券代號", "證券名稱", "最後交易日之收盤價格", "恢復買賣開始參考價"),
]
SPEC_BY_SOURCE = {s.source: s for s in SPECS}


def parse(spec: Spec, text: str) -> list[Action]:
    """解析區間查詢結果。查無資料回傳空串列（該區間可能真的沒有事件）。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"{spec.source} 不是 JSON：{text[:100]!r}") from e
    stat = str(data.get("stat", ""))
    if stat.lower() != "ok":
        if "沒有符合條件" in stat:
            return []
        raise ParseError(f"{spec.source}：{stat}")

    if "fields" in data:  # 證交所：欄位在最上層
        fields, rows = data.get("fields") or [], data.get("data") or []
    else:  # 櫃買中心：tables[0]
        tables = data.get("tables") or [{}]
        fields, rows = tables[0].get("fields") or [], tables[0].get("data") or []
    if not rows:
        return []
    cols = [spec.date_col, spec.code_col, spec.name_col, spec.prev_col, spec.ref_col]
    cols += [c for c in (spec.note_col, spec.theo_col) if c]
    missing = [c for c in cols if c not in fields]
    if missing:
        raise ParseError(f"{spec.source} 缺少欄位 {missing}，實際：{fields}")
    idx = {c: fields.index(c) for c in cols}

    actions = []
    for row in rows:
        code = str(row[idx[spec.code_col]]).strip()
        if not code.isascii() or not code.isalnum():
            continue
        ref = to_float(row[idx[spec.ref_col]])
        theo = to_float(row[idx[spec.theo_col]]) if spec.theo_col else None
        actions.append(Action(
            date=roc_date(str(row[idx[spec.date_col]])), market=spec.market, code=code,
            name=str(row[idx[spec.name_col]]).strip(), kind=spec.kind,
            prev_close=to_float(row[idx[spec.prev_col]]), ref_price=ref,
            theo_ref=theo if theo and theo != ref else None,
            note=str(row[idx[spec.note_col]]).strip() if spec.note_col else None,
        ))
    return actions
