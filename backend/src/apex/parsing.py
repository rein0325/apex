"""共用的解析工具：數字、民國日期、證券分類。"""

from __future__ import annotations

import datetime as dt
import re

_TAG = re.compile(r"<[^>]+>")
_NO_VALUE = {"", "-", "--", "---", "----", "X", "N/A"}


def to_float(value: object) -> float | None:
    """'1,234.50' → 1234.5；'--'、'---'、空字串 → None。"""
    if value is None:
        return None
    s = _TAG.sub("", str(value)).replace(",", "").strip()
    if s in _NO_VALUE:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(value: object) -> int | None:
    f = to_float(value)
    return None if f is None else int(round(f))


def roc_date(s: str) -> dt.date:
    """民國日期 '104/03/02'、'1040302' 或西元 '20150302' → date。"""
    digits = re.sub(r"\D", "", s)
    if len(digits) == 7:
        return dt.date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7]))
    if len(digits) == 8:
        return dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    raise ValueError(f"無法解析日期：{s!r}")


# 證券分類（見 docs/資料層設計.md §3）。順序重要：權證先判斷。
WARRANT = re.compile(r"(0[3-8]\d{4}|7\d{4,5})[A-Z]?")
_TYPES: list[tuple[str, re.Pattern[str]]] = [
    ("dr", re.compile(r"91\d{2}|91\d{4}")),  # 早期存託憑證用 4 碼（9103 美德醫療-DR），先於普通股判斷
    ("stock", re.compile(r"[1-9]\d{3}")),
    ("etf", re.compile(r"00\d{2,4}[A-Z]?")),
    ("preferred", re.compile(r"[1-9]\d{3}[A-Z]\d?")),
    ("etn", re.compile(r"02\d{3,4}[A-Z]?")),  # 020000、槓桿／反向 02001L
    ("reit", re.compile(r"01\d{3}T")),
]


def is_warrant(code: str) -> bool:
    return WARRANT.fullmatch(code) is not None


def classify(code: str) -> str:
    if is_warrant(code):
        return "warrant"
    for name, pattern in _TYPES:
        if pattern.fullmatch(code):
            return name
    return "other"
