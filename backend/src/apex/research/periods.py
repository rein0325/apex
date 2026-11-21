"""資料切分（docs/研究報告/策略研究計畫.md §2）：研究期可反覆使用，驗證期只跑定版設定，保留期最後只開一次。"""

from __future__ import annotations

import datetime as dt

PERIODS: dict[str, tuple[dt.date, dt.date | None]] = {
    "research": (dt.date(2016, 1, 4), dt.date(2022, 12, 30)),
    "validation": (dt.date(2023, 1, 2), dt.date(2024, 12, 31)),
    # 保留期固定到 2026-10-30：之後是模擬追蹤（紙上交易）的實際追蹤期，兩者不重疊（2026-11-19 決定）
    # 2026-11-21 已開啟一次（docs/研究報告/模型改進研究.md §5.4），之後不能再用來挑選或修改模型
    "holdout": (dt.date(2025, 1, 2), dt.date(2026, 10, 30)),
    "live": (dt.date(2026, 11, 2), None),  # 實際追蹤期：到資料最後一天，只用來追蹤，不用來挑選策略
    # 早期考試期（2026-11-22 回補）：沒人看過的過去資料；模型用研究期訓練好、不修改，只考一次
    # （docs/研究報告/大漲股研究.md）。2011～2012 為暖機，2015 為原本的暖機年
    "early": (dt.date(2013, 1, 2), dt.date(2014, 12, 31)),
}
LABELS = {"research": "研究期", "validation": "驗證期", "holdout": "保留期", "live": "實際追蹤期",
          "early": "早期考試期"}


class HoldoutLocked(RuntimeError):
    pass


def period_range(name: str, last_day: dt.date, unlock_holdout: bool = False,
                 unlock_early: bool = False) -> tuple[dt.date, dt.date]:
    """回傳期間起訖。保留期、早期考試期必須明確解鎖，避免研究過程中不小心看到。"""
    if name not in PERIODS:
        raise ValueError(f"未知期間 {name}，可用：{', '.join(PERIODS)}")
    if name in ("holdout", "live") and not unlock_holdout:
        raise HoldoutLocked(f"{LABELS[name]}已鎖定：保留期只在全部研究結束時開一次，實際追蹤期只用來追蹤；"
                            "確定要開請加 --unlock-holdout")
    if name == "early" and not unlock_early:
        raise HoldoutLocked("早期考試期已鎖定：設定定案後只考一次；確定要開請加 --unlock-early")
    start, end = PERIODS[name]
    return start, min(end or last_day, last_day)


def check_range(start: dt.date, end: dt.date, unlock_holdout: bool = False,
                unlock_early: bool = False) -> None:
    """自訂期間不得跨入保留期或早期考試期（除非解鎖）。"""
    if start <= PERIODS["early"][1] and not unlock_early:
        raise HoldoutLocked(f"期間起始日 {start} 落在早期考試期（{PERIODS['early'][1]} 以前）；"
                            "確定要開請加 --unlock-early")
    if end >= PERIODS["holdout"][0] and not unlock_holdout:
        raise HoldoutLocked(f"期間結束日 {end} 落在保留期（{PERIODS['holdout'][0]} 起）；"
                            "確定要開請加 --unlock-holdout")
