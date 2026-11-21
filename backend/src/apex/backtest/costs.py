"""交易成本與成交模擬參數（docs/回測框架.md §2）。

最低手續費 1 元、允許零股依使用者券商設定；手續費折扣尚未提供，暫用法定費率（無折扣）。
可在指令列以參數覆寫。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

# 台股漲跌幅限制 2015-06-01 起由 7% 放寬為 10%；之前的回測用 7% 判斷漲跌停
LIMIT_10PCT_FROM = dt.date(2015, 6, 1)
OLD_LIMIT_THRESHOLD = 0.065


@dataclass(frozen=True)
class CostModel:
    commission_rate: float = 0.001425  # 法定上限 0.1425%
    commission_discount: float = 1.0  # 券商折扣，例如 0.28 代表 2.8 折
    min_commission: float = 1.0  # 每筆最低手續費（元），依使用者券商設定
    tax_stock: float = 0.003  # 證交稅：股票賣出 0.3%
    tax_etf: float = 0.001  # ETF、受益憑證賣出 0.1%
    tax_bond_etf: float = 0.0  # 債券 ETF 停徵證交稅（至 2026-12-31）
    slippage_bps: float = 10.0  # 每邊滑價（基點），買進加價、賣出減價
    lot_size: int = 1  # 1＝允許零股（使用者會買零股）；1000＝只能整張
    max_participation: float = 0.1  # 單筆成交金額上限：當日成交金額的比例
    limit_threshold: float = 0.095  # 開盤相對前收（還原）漲跌幅達此值視為漲跌停，買不到／賣不掉
    # 下市時以最後收盤價的比例轉為現金（合併換股通常接近 1，下市清算可能遠低於 1）
    delist_recovery: float = 1.0

    def limit_at(self, date: dt.date) -> float:
        """date 當天視為漲跌停的開盤漲跌幅（2015-06-01 前漲跌幅限制為 7%）。"""
        return OLD_LIMIT_THRESHOLD if date < LIMIT_10PCT_FROM else self.limit_threshold

    def commission(self, value: float) -> float:
        if value <= 0:
            return 0.0
        return max(self.min_commission, value * self.commission_rate * self.commission_discount)

    def tax(self, value: float, sec_type: str, code: str) -> float:
        if sec_type == "etf":
            rate = self.tax_bond_etf if code.endswith("B") else self.tax_etf
        elif sec_type in ("stock", "dr", "preferred"):
            rate = self.tax_stock
        else:
            rate = self.tax_etf
        return value * rate

    def buy_price(self, price: float) -> float:
        return price * (1 + self.slippage_bps / 10_000)

    def sell_price(self, price: float) -> float:
        return price * (1 - self.slippage_bps / 10_000)
