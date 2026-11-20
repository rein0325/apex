"""網站共用的風險提醒設定（docs/研究報告/出場規則研究.md）。

2026-11-21 出場規則研究的結論與使用者決定：
- 個股停損、移動停損：回測兩段期間都讓結果變差，不採用。
- 大盤轉壞當天減碼：研究期有效，但驗證期沒有觸發、無法確認；使用者選擇不採用（選項二）。
所以網站只保留帳戶回撤提醒，以及持有滿 60 個交易日（模型預測期間）重新評估的提醒。
市場狀態是否「不好」只用來描述大盤，不用來建議減碼。這些設定不含個股，放在公開的 data.json。
"""

from __future__ import annotations

import math

RULES = {
    "review_days": 60,  # 持有滿 60 個交易日（模型預測期間）重新評估
}


def bad_regime(regime: str | None, ma_gap: float | None) -> bool:
    """空頭，或高波動且指數在 200 日均線下（與研究的 D2／X 規則相同）。"""
    gap = None if ma_gap is None or (isinstance(ma_gap, float) and math.isnan(ma_gap)) else ma_gap
    return regime == "空頭" or (regime == "高波動" and gap is not None and gap < 0)


def site_rules(regime: str | None, ma_gap: float | None) -> dict:
    return {**RULES, "bad_now": bad_regime(regime, ma_gap)}
