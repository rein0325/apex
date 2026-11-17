"""資料品質檢查（docs/資料層設計.md §5）。

只要有 error，該日該市場標記為 failed，下游不得使用。warning 只記錄，供人工檢查。
"""

from __future__ import annotations

import datetime as dt
import statistics

from apex.db import MARKETS, Database

ROW_COUNT_MIN_RATIO = 0.8
ROW_COUNT_LOOKBACK = 20
OHLC_ERROR_RATIO = 0.01
NO_TRADE_WARN_RATIO = 0.30
EXTREME_MOVE = 0.105

Result = tuple[str, str, str, str]  # (market, check_name, severity, detail)


def _examples(rows: list[tuple], limit: int = 5) -> str:
    return "、".join(str(r[0]) for r in rows[:limit]) + ("…" if len(rows) > limit else "")


def check_date(db: Database, date: dt.date) -> list[Result]:
    con = db.con
    results: list[Result] = []

    for market in MARKETS:
        n = con.execute("SELECT count(*) FROM daily_quotes WHERE date = ? AND market = ?",
                        [date, market]).fetchone()[0]
        if n == 0:
            results.append((market, "empty_market", "error", "交易日 0 筆"))
            continue

        # 筆數與前 20 個 ok 交易日中位數比較
        prev = con.execute("""
            SELECT n_rows FROM ingest_status
            WHERE market = ? AND status = 'ok' AND date < ? ORDER BY date DESC LIMIT ?
        """, [market, date, ROW_COUNT_LOOKBACK]).fetchall()
        if len(prev) >= 5:
            median = statistics.median(r[0] for r in prev)
            if n < median * ROW_COUNT_MIN_RATIO:
                results.append((market, "row_count", "error",
                                f"{n} 筆，低於近 {len(prev)} 日中位數 {median:.0f} 的 80%"))

        # 開高低收一致性（只看有成交的列）
        traded, bad = con.execute("""
            SELECT count(*),
                   count(*) FILTER (WHERE NOT (low > 0 AND low <= least(open, close)
                                               AND high >= greatest(open, close)))
            FROM daily_quotes WHERE date = ? AND market = ? AND close IS NOT NULL
        """, [date, market]).fetchone()
        if bad:
            rows = con.execute("""
                SELECT code FROM daily_quotes WHERE date = ? AND market = ? AND close IS NOT NULL
                  AND NOT (low > 0 AND low <= least(open, close) AND high >= greatest(open, close))
            """, [date, market]).fetchall()
            severity = "error" if bad / max(traded, 1) > OHLC_ERROR_RATIO else "warning"
            results.append((market, "ohlc_consistency", severity, f"{bad} 筆不一致：{_examples(rows)}"))

        # 普通股無成交比例
        stocks, no_trade = con.execute("""
            SELECT count(*), count(*) FILTER (WHERE close IS NULL)
            FROM daily_quotes WHERE date = ? AND market = ? AND sec_type = 'stock'
        """, [date, market]).fetchone()
        if stocks and no_trade / stocks > NO_TRADE_WARN_RATIO:
            results.append((market, "no_trade_ratio", "warning",
                            f"普通股 {no_trade}/{stocks} 無成交"))

        # 普通股相對前一交易日收盤的極端漲跌
        prev_day = db.previous_trading_days(date, 1)
        if prev_day:
            rows = con.execute("""
                SELECT t.code, t.close / p.close - 1 AS ret
                FROM daily_quotes t JOIN daily_quotes p
                  ON p.code = t.code AND p.market = t.market AND p.date = ?
                WHERE t.date = ? AND t.market = ? AND t.sec_type = 'stock'
                  AND t.close IS NOT NULL AND p.close IS NOT NULL
                  AND abs(t.close / p.close - 1) > ?
                ORDER BY abs(ret) DESC
            """, [prev_day[0], date, market, EXTREME_MOVE]).fetchall()
            if rows:
                results.append((market, "extreme_move", "warning",
                                f"{len(rows)} 檔漲跌超過 {EXTREME_MOVE:.1%}：{_examples(rows)}"))

        unknown = con.execute("""
            SELECT code FROM daily_quotes WHERE date = ? AND market = ? AND sec_type = 'other'
        """, [date, market]).fetchall()
        if unknown:
            results.append((market, "unknown_type", "warning",
                            f"{len(unknown)} 個無法分類代號：{_examples(unknown)}"))

    dup = con.execute("""
        SELECT code FROM daily_quotes WHERE date = ? GROUP BY code HAVING count(DISTINCT market) > 1
    """, [date]).fetchall()
    if dup:
        results.append(("ALL", "cross_market_duplicate", "error",
                        f"{len(dup)} 個代號同時出現在上市與上櫃：{_examples(dup)}"))
    return results


def has_error(results: list[Result], market: str) -> list[Result]:
    return [r for r in results if r[2] == "error" and r[0] in (market, "ALL")]
