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
    st = con.execute("SELECT status, count(*) FROM securities GROUP BY 1 ORDER BY 2 DESC").fetchall()
    snap = con.execute("SELECT max(month) FROM listing_snapshots").fetchone()[0]
    print("掛牌狀態：" + "、".join(f"{s} {n}" for s, n in st) + f"（一覽表 {snap or '未同步'}）")
    act = con.execute("""
        SELECT kind, count(*), min(date), max(date) FROM corporate_actions GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    if act:
        print("公司行動：" + "、".join(f"{k} {n}（{a}～{b}）" for k, n, a, b in act))
    for ds, ok, failed, first, last in con.execute("""
        SELECT dataset, count(*) FILTER (WHERE status = 'ok'), count(*) FILTER (WHERE status = 'failed'),
               min(date) FILTER (WHERE status = 'ok'), max(date) FILTER (WHERE status = 'ok')
        FROM dataset_status GROUP BY 1 ORDER BY 1
    """).fetchall():
        print(f"{ds}：ok {ok}、failed {failed}（{first}～{last}，市場×日）")
    warn = con.execute("""
        SELECT check_name, count(*) FROM dq_results WHERE severity = 'warning' GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    if warn:
        print("品質警告（天數）：" + "、".join(f"{c} {n}" for c, n in warn))


def run_backtest(db: Database, args: argparse.Namespace) -> int:
    from apex import backtest
    from apex.backtest.strategies import from_spec

    costs = backtest.CostModel(commission_discount=args.discount, min_commission=args.min_fee,
                               slippage_bps=args.slippage_bps, lot_size=args.lot,
                               delist_recovery=args.delist_recovery)
    data = backtest.load(db, args.start, args.end)
    result = backtest.run(data, from_spec(args.strategy), args.start, args.end, args.capital, args.freq,
                          costs)
    bench = db.con.execute(
        "SELECT date, close FROM index_daily WHERE index_code = 'TAIEX_TR' ORDER BY date").df()
    benchmark = None
    if len(bench):
        benchmark = bench.assign(date=lambda d: d["date"].astype("datetime64[ns]")).set_index("date")["close"]
    m = backtest.summarize(result.equity, result.trades, benchmark, result.cash)
    pct = {"total_return", "cagr", "volatility", "max_drawdown", "exposure", "benchmark_cagr", "excess_cagr",
           "win_rate", "cost_drag"}
    print(f"策略 {args.strategy}，{args.start}～{args.end}，本金 {args.capital:,.0f}，再平衡 {args.freq}")
    print(f"期末資產 {result.equity.iloc[-1]:,.0f}")
    for k, v in m.items():
        print(f"  {k:<20}{v:>12.2%}" if k in pct else f"  {k:<20}{v:>12,.2f}")
    print("逐年報酬（基準：加權報酬指數）：")
    print(backtest.yearly_returns(result.equity, benchmark).to_string(float_format=lambda x: f"{x:7.2%}"))
    if result.events:
        print(f"事件 {len(result.events)} 筆，前 10 筆：")
        for e in result.events[:10]:
            print("  " + e)
    return 0


def run_research(db: Database, args: argparse.Namespace) -> int:
    from apex.backtest import CostModel
    from apex.research.lab import Lab
    from apex.research.periods import LABELS, check_range, period_range
    from apex.research.strategies import build, family_of

    last_day = db.con.execute("SELECT max(date) FROM daily_quotes").fetchone()[0]
    if args.start:
        start, end = args.start, args.end or last_day
        check_range(start, end, args.unlock_holdout)
        period = "custom"
    else:
        start, end = period_range(args.period, last_day, args.unlock_holdout)
        period = args.period
    freqs = args.freq or ["M"]
    costs = CostModel(commission_discount=args.discount, min_commission=args.min_fee,
                      slippage_bps=args.slippage_bps, lot_size=args.lot)
    lab = Lab(db, start, end, args.capital, costs)
    print(f"{LABELS.get(period, '自訂期間')} {start}～{end}")
    for spec in args.strategy:
        for freq in freqs:
            report = lab.evaluate(spec, build(spec, lab.features), start, end, freq, family=family_of(spec),
                                  period=period, random_runs=args.random, note=args.note)
            print()
            print(report.text())
    return 0


def print_trials(db: Database, args: argparse.Namespace) -> int:
    from apex.research import trials

    df = trials.trials(db, args.period)
    if df.empty:
        print("沒有試驗紀錄")
        return 0
    real = df[df["family"] != "base"]
    print(f"{args.period}：共 {len(df)} 筆試驗，其中策略 {len(real)} 筆（基準不計入 DSR）")
    top = df.sort_values("sharpe", ascending=False).head(args.top)
    for _, r in top.iterrows():
        d = trials.deflate(db, int(r["id"]), args.period) if r["family"] != "base" else {}
        dsr = f"{d['dsr']:.2f}" if d else "—"
        print(f"#{r['id']:<4} {r['strategy']:<24} {r['freq']:<4} 本金 {r['capital']:>10,.0f}  "
              f"年化 {r['cagr']:+7.2%}  Sharpe {r['sharpe']:5.2f}  回撤 {r['max_drawdown']:+7.1%}  DSR {dsr}")
    return 0


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

    p = sub.add_parser("actions", help="同步除權息、減資、變更面額並重建還原因子")
    p.add_argument("--start-year", required=True, type=int)
    p.add_argument("--end-year", required=True, type=int)
    p.add_argument("--refetch", action="store_true", help="重新下載原始資料")
    p.add_argument("--reprocess", action="store_true", help="用已存的原始檔重新解析")

    p = sub.add_parser("chips", help="擷取三大法人、融資融券（可中斷續跑）")
    p.add_argument("--start", required=True, type=_date)
    p.add_argument("--end", required=True, type=_date)
    p.add_argument("--dataset", action="append", choices=["institutional", "margin"], help="預設全部")
    p.add_argument("--refetch", action="store_true", help="重新下載原始資料")
    p.add_argument("--reprocess", action="store_true", help="用已存的原始檔重新解析")

    p = sub.add_parser("fundamentals", help="同步月營收與季財報（公開資訊觀測站）")
    p.add_argument("--start-year", type=int, default=2015)
    p.add_argument("--refetch", action="store_true", help="重新下載原始資料")
    p.add_argument("--reprocess", action="store_true", help="用已存的原始檔重新解析")

    p = sub.add_parser("backtest", help="執行回測並與加權報酬指數比較")
    p.add_argument("--strategy", required=True, help="buyhold:0050、equal:50、momentum:20")
    p.add_argument("--start", required=True, type=_date)
    p.add_argument("--end", required=True, type=_date)
    p.add_argument("--capital", type=float, default=1_000_000)
    p.add_argument("--freq", default="M", choices=["D", "W", "2W", "M", "once"], help="再平衡頻率")
    p.add_argument("--discount", type=float, default=1.0, help="手續費折扣，例如 0.28")
    p.add_argument("--min-fee", type=float, default=1.0, help="每筆最低手續費")
    p.add_argument("--slippage-bps", type=float, default=10.0)
    p.add_argument("--lot", type=int, default=1, help="1＝允許零股、1000＝整張")
    p.add_argument("--delist-recovery", type=float, default=1.0, help="下市時以最後收盤價的多少比例轉現金")

    p = sub.add_parser("research", help="策略研究：標準報表＋試驗紀錄（docs/策略研究計畫.md）")
    p.add_argument("--strategy", required=True, action="append", help="可重複，例如 equal:50")
    p.add_argument("--period", default="research", choices=["research", "validation", "holdout"])
    p.add_argument("--start", type=_date, help="自訂起日（覆蓋 --period）")
    p.add_argument("--end", type=_date, help="自訂迄日")
    p.add_argument("--freq", action="append", choices=["W", "2W", "M", "once"], help="可重複；預設 M")
    p.add_argument("--capital", type=float, default=300_000)
    p.add_argument("--random", type=int, default=0, help="隨機選股對照次數，例如 100")
    p.add_argument("--unlock-holdout", action="store_true", help="開啟保留期（全部研究結束才用）")
    p.add_argument("--note", default="")
    p.add_argument("--discount", type=float, default=1.0)
    p.add_argument("--min-fee", type=float, default=1.0)
    p.add_argument("--slippage-bps", type=float, default=10.0)
    p.add_argument("--lot", type=int, default=1)

    p = sub.add_parser("trials", help="列出試驗紀錄與 Deflated Sharpe")
    p.add_argument("--period", default="research")
    p.add_argument("--top", type=int, default=20)

    p = sub.add_parser("indices", help="同步加權指數與報酬指數")
    p.add_argument("--start", required=True, help="YYYY-MM")
    p.add_argument("--end", required=True, help="YYYY-MM")

    sub.add_parser("holidays", help="同步今年與明年的開休市日期")

    p = sub.add_parser("finmind", help="以 FinMind 補齊已下市櫃公司的月營收與財報")
    p.add_argument("--refetch", action="store_true")

    p = sub.add_parser("listings", help="同步產業別、上市櫃日與終止上市櫃清單")
    p.add_argument("--refetch", action="store_true", help="重新下載（覆蓋本月的一覽表）")

    p = sub.add_parser("recheck", help="只重跑品質檢查（不重新解析）")
    p.add_argument("--start", required=True, type=_date)
    p.add_argument("--end", required=True, type=_date)

    sub.add_parser("update", help="每日更新：同步公司行動，補齊近一個月未成功的交易日")
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
        if args.cmd == "backtest":
            return run_backtest(db, args)
        if args.cmd == "research":
            return run_research(db, args)
        if args.cmd == "trials":
            return print_trials(db, args)
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
        if args.cmd == "chips":
            c = pipe.sync_daily(args.start, args.end, args.dataset, args.refetch, args.reprocess)
            print(f"成功 {c['ok']}、失敗 {c['failed']}（市場×資料集×日）")
            return 0 if not c["failed"] else 1
        if args.cmd == "indices":
            print(f"指數 {pipe.sync_indices(_month_start(args.start), _month_end(args.end))} 個交易日")
            return 0
        if args.cmd == "holidays":
            print(f"開休市日期 {pipe.sync_holidays()} 筆")
            return 0
        if args.cmd == "finmind":
            c = pipe.sync_finmind(args.refetch)
            db.refresh_securities()
            print(f"補齊月營收 {c['revenue']}、損益 {c['income']}、資產負債 {c['balance']} 筆")
            return 0
        if args.cmd == "fundamentals":
            c = pipe.sync_fundamentals(args.start_year, refetch=args.refetch, reprocess=args.reprocess)
            print(f"月營收 {c['revenue']} 筆、財報 {c['financials']} 筆、尚未公告 {c['pending']} 個期間")
            return 0
        if args.cmd == "actions":
            n = pipe.sync_actions(args.start_year, args.end_year, refetch=args.refetch,
                                 reprocess=args.reprocess)
            print(f"公司行動 {n} 筆")
            return 0
        if args.cmd == "listings":
            print(f"證券主檔 {pipe.sync_listings(refetch=args.refetch)} 筆")
            return 0
        if args.cmd == "recheck":
            print(f"重新檢查 {pipe.recheck(args.start, args.end)} 個交易日")
            return 0
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
