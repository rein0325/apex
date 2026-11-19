"""各資料來源的端點與解析。解析函式只處理文字，不做網路請求，方便測試。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class Quote:
    date: dt.date
    market: str  # 'TWSE' | 'TPEX'
    code: str
    name: str
    sec_type: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None  # 股
    value: int | None  # 元
    trades: int | None
    is_managed: bool = False


class NotPublished(Exception):
    """來源回應「沒有資料」：可能是假日，或當日資料尚未發布。"""


class ParseError(Exception):
    """回應格式不符預期，需要人工檢查。"""
