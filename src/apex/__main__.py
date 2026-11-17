"""指令列：python -m apex <指令>（見 docs/資料層設計.md §6）。"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from apex.config import load_settings
from apex.db import Database
from apex.http import HttpClient
from apex.pipeline import Pipeline, http_fetcher
from apex.raw_store import RawStore


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _month_start(s: str) -> dt.date:
    return dt.date.fromisoformat(s + "-01") if len(s) == 7 else dt.date.fromisoformat(s)


def _month_end(s: str) -> dt.date:
    if len(s) == 7:
        first = dt.date.fromisoformat(s + "-01")
        return (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return dt.date.fromisoformat(s)


def print_status(db: Database) -> None:
    con = db.con
    cal = con.execute("SELECT min(date), max(date), count(*) FROM trading_days").fetchone()
    print(f"交易日曆：{cal[0]} ～ {cal[1]}，共 {cal[2]} 天")
    for market, ok, failed, last_ok in con.execute("""
        SELECT market, count(*) FILTER (WHERE status = 'ok'), count(*) FILTER (WHERE status = 'failed'),
               max(date) FILTER (WHERE status = 'ok')
        FROM ingest_status GROUP BY market ORDER BY market
    """).fetchall():
        print(f"{market}：ok {ok} 天、failed {failed} 天，最新 ok 資料日 {last_ok}")
    missing = con.execute("""
        SELECT count(*) FROM trading_days t
        WHERE t.date >= (SELECT min(date) FROM ingest_status)
          AND NOT EXISTS (SELECT 1 FROM ingest_status s WHERE s.date = t.date AND s.status = 'ok')
    """).fetchone()[0]
    print(f"範圍內尚未成功的交易日：{missing}")
    for date, market, message in con.execute("""
        SELECT date, market, message FROM ingest_status WHERE status = 'failed' ORDER BY date DESC LIMIT 10
    """).fetchall():
        print(f"  failed {date} {market}：{message}")
    sec = con.execute("SELECT sec_type, count(*) FROM securities GROUP BY 1 ORDER BY 2 DESC").fetchall()
    print("證券主檔：" + "、".join(f"{t} {n}" for t, n in sec))
    warn = con.execute("""
        SELECT check_name, count(*) FROM dq_results WHERE severity = 'warning' GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    if warn:
        print("品質警告（天數）：" + "、".join(f"{c} {n}" for c, n in warn))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apex", description="台股資料層")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("calendar", help="同步交易日曆")
    p.add_argument("--start", required=True, help="YYYY-MM")
    p.add_argument("--end", required=True, help="YYYY-MM")

    p = sub.add_parser("ingest", help="擷取單一交易日")
    p.add_argument("--date", required=True, type=_date)
    p.add_argument("--refetch", action="store_true", help="忽略已存的原始檔，重新下載")

    p = sub.add_parser("backfill", help="回補區間（可中斷續跑）")
    p.add_argument("--start", required=True, type=_date)
    p.add_argument("--end", required=True, type=_date)
    p.add_argument("--refetch", action="store_true", help="重新下載原始資料")
    p.add_argument("--reprocess", action="store_true", help="用已存的原始檔重新解析（不重新下載）")

    sub.add_parser("update", help="每日更新：補齊近一個月未成功的交易日")
    sub.add_parser("status", help="資料狀態摘要")

    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    settings = load_settings()
    db = Database(settings.db_path)
    try:
        if args.cmd == "status":
            print_status(db)
            return 0
        client = HttpClient(settings.request_delay_sec, settings.max_retries, settings.timeout_sec)
        pipe = Pipeline(db, RawStore(settings.raw_dir), http_fetcher(client))
        if args.cmd == "calendar":
            pipe.sync_calendar(_month_start(args.start), _month_end(args.end))
            return 0
        if args.cmd == "ingest":
            pipe.sync_calendar(args.date, args.date)
            if args.date not in pipe.db.trading_days(args.date, args.date):
                print(f"{args.date} 不是交易日（或交易日曆尚未包含此日）")
                return 1
            r = pipe.ingest_date(args.date, args.refetch)
            db.refresh_securities()
            print(f"{r.date}：{r.status} {r.rows} {'；'.join(r.messages)}")
            return 0 if r.ok else 1
        if args.cmd == "backfill":
            results = pipe.backfill(args.start, args.end, args.refetch, reprocess=args.reprocess)
        else:
            results = pipe.update()
        failed = [r for r in results if not r.ok]
        print(f"處理 {len(results)} 個交易日，失敗 {len(failed)} 個")
        for r in failed:
            print(f"  {r.date}：{'；'.join(r.messages)}")
        return 0 if not failed else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
