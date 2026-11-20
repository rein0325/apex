"""出場規則與建議投入比例（docs/研究報告/出場規則研究.md）。

數字來自回測研究（研究期 2018–2022 選定、驗證期 2023–2024 確認）。
網站的「我的持股」與「候選股票」頁都用這裡的設定。
這些是一般性的風險控管規則，不含個股，所以放在公開的 data.json。
"""

from __future__ import annotations

import math

# 由出場規則研究決定（見文件）；改這裡的數字要同步更新文件
RULES = {
    "stop": 0.15,  # 停損：收盤價比平均成本低 15%
    "trail": 0.20,  # 移動停損：收盤價比買進後最高收盤低 20%
    "review_days": 60,  # 持有滿 60 個交易日（模型預測期間）重新評估
    "bad_exposure": 0.5,  # 市場狀態不好時，股票最多占總資產的比例
}


def bad_regime(regime: str | None, ma_gap: float | None) -> bool:
    """空頭，或高波動且指數在 200 日均線下（與研究的 D2／X 規則相同）。"""
    gap = None if ma_gap is None or (isinstance(ma_gap, float) and math.isnan(ma_gap)) else ma_gap
    return regime == "空頭" or (regime == "高波動" and gap is not None and gap < 0)


def site_rules(regime: str | None, ma_gap: float | None) -> dict:
    bad = bad_regime(regime, ma_gap)
    return {**RULES, "bad_now": bad, "exposure": RULES["bad_exposure"] if bad else 1.0}
