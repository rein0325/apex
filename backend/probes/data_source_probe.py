"""資料來源探測腳本（只研究，不建資料庫）

目的：實測候選資料來源是否可用、回傳格式、與官方數字是否一致，
作為「資料來源評估.md」的依據。對應系統規格書 STEP 01／§8 第 1–2 項。

只用 Python 標準函式庫，不需安裝套件。

用法：
    python probes/data_source_probe.py
    python probes/data_source_probe.py --date 2026-11-13 --hist-date 2015-03-02

可選環境變數：
    FINMIND_TOKEN  FinMind 註冊會員 token（有的話會順便查詢用量上限）

輸出：
    probes/output/<執行時間>/report.md   探測報告
    probes/output/<執行時間>/results.json 結構化結果
    probes/output/<執行時間>/raw/        每個請求的原始回應（可追溯）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

USER_AGENT = "Mozilla/5.0 (data-source-probe; personal research)"
EXCHANGE_DELAY_SEC = 3.0  # 交易所網站沒有公開頻率限制，保守間隔
FINMIND_DELAY_SEC = 1.0
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

# 抽樣比對用股票：上市、上櫃、ETF 各取幾檔
SAMPLE_TWSE = ["2330", "2317", "2454", "2882", "0050"]
SAMPLE_TPEX = ["6488", "5483", "8069", "3105", "6547"]


# ---------------------------------------------------------------------------
# 基礎工具
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    source: str
    ok: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    elapsed_sec: float = 0.0


class Http:
    """簡單的 HTTP 客戶端：限速、記錄原始回應。"""

    def __init__(self, raw_dir: Path):
        self.raw_dir = raw_dir
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._last_call: dict[str, float] = {}
        self._seq = 0
        self.request_log: list[dict[str, Any]] = []

    def _throttle(self, bucket: str, delay: float) -> None:
        last = self._last_call.get(bucket)
        if last is not None:
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[bucket] = time.monotonic()

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        *,
        bucket: str,
        delay: float,
        data: dict[str, str] | None = None,
        label: str = "",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        self._throttle(bucket, delay)
        full_url = url + ("?" + urllib.parse.urlencode(params) if params else "")
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(full_url, data=body, method="POST" if body else "GET")
        req.add_header("User-Agent", USER_AGENT)
        for k, v in (headers or {}).items():
            req.add_header(k, v)

        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status = resp.status
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            status = e.code
            text = e.read().decode("utf-8", errors="replace")
        elapsed = time.monotonic() - start

        self._seq += 1
        safe_label = re.sub(r"[^\w\-]+", "_", label or bucket)[:60]
        raw_path = self.raw_dir / f"{self._seq:03d}_{safe_label}.txt"
        # 原始回應保存時不記錄 token
        logged_url = re.sub(r"token=[^&]+", "token=REDACTED", full_url)
        raw_path.write_text(f"# {logged_url}\n# status={status}\n{text}", encoding="utf-8")
        self.request_log.append(
            {"seq": self._seq, "url": logged_url, "status": status,
             "elapsed_sec": round(elapsed, 2), "bytes": len(text), "raw": raw_path.name}
        )
        return status, text

    def get_json(self, *args, **kwargs) -> tuple[int, Any]:
        status, text = self.get(*args, **kwargs)
        try:
            return status, json.loads(text)
        except json.JSONDecodeError:
            return status, {"_non_json": text[:300]}


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    s = re.sub(r"<[^>]+>", "", str(value)).replace(",", "").strip()
    if s in ("", "--", "---", "X", "除權", "除息"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_to_date(s: str) -> str:
    """民國日期（1151113 或 115/11/13）轉西元 YYYY-MM-DD。"""
    digits = re.sub(r"\D", "", s)
    if len(digits) == 7:
        return f"{int(digits[:3]) + 1911:04d}-{digits[3:5]}-{digits[5:7]}"
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return s


def find_col(fields: list[str], *keywords: str) -> int | None:
    for i, f in enumerate(fields):
        name = re.sub(r"\s+", "", str(f))
        if all(k in name for k in keywords):
            return i
    return None


def run_check(name: str, source: str, fn, *args) -> CheckResult:
    start = time.monotonic()
    try:
        ok, summary, details = fn(*args)
    except Exception as e:  # 探測腳本：任何失敗都記錄下來，不中斷其他檢查
        ok, summary, details = False, f"例外：{type(e).__name__}: {e}", {
            "traceback": traceback.format_exc(limit=3)}
    result = CheckResult(name, source, ok, summary, details, round(time.monotonic() - start, 1))
    mark = "OK " if ok else "NG "
    print(f"[{mark}] {name}：{summary}")
    return result


# ---------------------------------------------------------------------------
# 官方交易所資料
# ---------------------------------------------------------------------------


def twse_openapi_latest(http: Http):
    status, data = http.get_json(
        "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
        bucket="twse", delay=EXCHANGE_DELAY_SEC, label="twse_openapi_STOCK_DAY_ALL")
    if status != 200 or not isinstance(data, list):
        return False, f"HTTP {status}", {}
    dates = sorted({roc_to_date(r.get("Date", "")) for r in data})
    return True, f"{len(data)} 檔，資料日期 {', '.join(dates)}", {
        "count": len(data), "dates": dates, "fields": list(data[0].keys()) if data else []}


def tpex_openapi_latest(http: Http):
    status, data = http.get_json(
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes",
        bucket="tpex", delay=EXCHANGE_DELAY_SEC, label="tpex_openapi_mainboard_quotes")
    if status != 200 or not isinstance(data, list):
        return False, f"HTTP {status}", {}
    date_key = next((k for k in (data[0] if data else {}) if "Date" in k), None)
    dates = sorted({roc_to_date(str(r.get(date_key, ""))) for r in data}) if date_key else []
    return True, f"{len(data)} 檔，資料日期 {', '.join(dates)}", {
        "count": len(data), "dates": dates, "fields": list(data[0].keys()) if data else []}


def twse_web_daily(http: Http, date: str) -> dict[str, dict[str, float | None]]:
    """證交所網站：指定日期全市場收盤行情。回傳 {代號: {close, volume}}。"""
    status, data = http.get_json(
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
        {"date": date.replace("-", ""), "type": "ALLBUT0999", "response": "json"},
        bucket="twse", delay=EXCHANGE_DELAY_SEC, label=f"twse_MI_INDEX_{date}")
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    for table in data.get("tables", []):
        fields = table.get("fields", [])
        i_code, i_close = find_col(fields, "證券代號"), find_col(fields, "收盤價")
        i_vol = find_col(fields, "成交股數")
        if i_code is not None and i_close is not None:
            return {
                row[i_code].strip(): {
                    "close": to_float(row[i_close]),
                    "volume": to_float(row[i_vol]) if i_vol is not None else None}
                for row in table.get("data", [])
            }
    raise RuntimeError(f"找不到個股行情表（stat={data.get('stat')}）")


def tpex_web_daily(http: Http, date: str) -> dict[str, dict[str, float | None]]:
    """櫃買中心網站：指定日期上櫃收盤行情。"""
    status, data = http.get_json(
        "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes",
        {"date": date.replace("-", "/"), "response": "json"},
        bucket="tpex", delay=EXCHANGE_DELAY_SEC, label=f"tpex_dailyQuotes_{date}")
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    for table in data.get("tables", []):
        fields = table.get("fields", [])
        i_code, i_close = find_col(fields, "代號"), find_col(fields, "收盤")
        i_vol = find_col(fields, "成交股數")
        if i_code is not None and i_close is not None:
            return {
                row[i_code].strip(): {
                    "close": to_float(row[i_close]),
                    "volume": to_float(row[i_vol]) if i_vol is not None else None}
                for row in table.get("data", [])
            }
    raise RuntimeError("找不到上櫃行情表")


def exchange_history(http: Http, hist_date: str):
    twse = twse_web_daily(http, hist_date)
    tpex = tpex_web_daily(http, hist_date)
    ok = len(twse) > 0 and len(tpex) > 0
    return ok, f"{hist_date}：上市 {len(twse)} 筆、上櫃 {len(tpex)} 筆（含 ETF 等）", {
        "twse_count": len(twse), "tpex_count": len(tpex),
        "twse_sample": dict(list(twse.items())[:3]), "tpex_sample": dict(list(tpex.items())[:3])}


def twse_t86_history(http: Http, hist_date: str):
    status, data = http.get_json(
        "https://www.twse.com.tw/rwd/zh/fund/T86",
        {"date": hist_date.replace("-", ""), "selectType": "ALLBUT0999", "response": "json"},
        bucket="twse", delay=EXCHANGE_DELAY_SEC, label=f"twse_T86_{hist_date}")
    rows = data.get("data", []) if isinstance(data, dict) else []
    ok = status == 200 and data.get("stat") == "OK" and len(rows) > 0
    return ok, f"{hist_date}：{len(rows)} 筆三大法人資料（stat={data.get('stat')}）", {
        "count": len(rows), "fields": data.get("fields", [])}


# ---------------------------------------------------------------------------
# FinMind
# ---------------------------------------------------------------------------


def finmind(http: Http, token: str | None, **params: str) -> tuple[int, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    label = "finmind_" + "_".join(f"{v}" for v in params.values())
    return http.get_json(FINMIND_URL, params, bucket="finmind", delay=FINMIND_DELAY_SEC,
                         label=label, headers=headers)


def finmind_full_market(http: Http, token: str | None, date: str):
    status, data = finmind(http, token, dataset="TaiwanStockPrice", start_date=date, end_date=date)
    rows = data.get("data", []) if isinstance(data, dict) else []
    if rows:
        return True, f"可一次取得全市場：{len(rows)} 筆", {"count": len(rows)}
    return False, f"不可一次取得全市場（{data.get('msg', status)}）", {"response": data}


def finmind_vs_official(http: Http, token: str | None, date: str):
    official = {**twse_web_daily(http, date), **tpex_web_daily(http, date)}
    compared, mismatches, missing = 0, [], []
    for stock in SAMPLE_TWSE + SAMPLE_TPEX:
        status, data = finmind(http, token, dataset="TaiwanStockPrice", data_id=stock,
                               start_date=date, end_date=date)
        rows = data.get("data", []) if isinstance(data, dict) else []
        if not rows or stock not in official:
            missing.append({"stock": stock, "finmind": bool(rows), "official": stock in official})
            continue
        fm_close, off_close = rows[0]["close"], official[stock]["close"]
        fm_vol, off_vol = rows[0]["Trading_Volume"], official[stock]["volume"]
        compared += 1
        if off_close is None or abs(fm_close - off_close) > 1e-6 or (
                off_vol is not None and abs(fm_vol - off_vol) > 0.5):
            mismatches.append({"stock": stock, "finmind_close": fm_close, "official_close": off_close,
                               "finmind_volume": fm_vol, "official_volume": off_vol})
    ok = compared > 0 and not mismatches
    summary = (f"{date} 比對 {compared} 檔，收盤價／成交股數不一致 {len(mismatches)} 檔，"
               f"缺資料 {len(missing)} 檔")
    return ok, summary, {"mismatches": mismatches, "missing": missing}


def finmind_delisted_history(http: Http, token: str | None):
    status, data = finmind(http, token, dataset="TaiwanStockDelisting")
    rows = data.get("data", []) if isinstance(data, dict) else []
    # 挑 2018–2024 年下市的普通股（4 碼數字、非 0 開頭）
    candidates = [r for r in rows
                  if re.fullmatch(r"[1-9]\d{3}", r["stock_id"]) and "2018" <= r["date"][:4] <= "2024"]
    candidates.sort(key=lambda r: r["date"])
    picks = candidates[:: max(1, len(candidates) // 3)][:3]
    results = []
    for r in picks:
        end = r["date"]
        start = (dt.date.fromisoformat(end) - dt.timedelta(days=365)).isoformat()
        _, price = finmind(http, token, dataset="TaiwanStockPrice", data_id=r["stock_id"],
                           start_date=start, end_date=end)
        prices = price.get("data", []) if isinstance(price, dict) else []
        results.append({"stock_id": r["stock_id"], "name": r["stock_name"],
                        "delisted": end, "price_rows_last_year": len(prices),
                        "last_trade": prices[-1]["date"] if prices else None})
    # 停止買賣常早於正式下市日，因此只檢查「有歷史股價」，最後交易日需與官方人工核對
    ok = bool(results) and all(x["price_rows_last_year"] > 0 for x in results)
    return ok, f"下市清單 {len(rows)} 筆；抽樣（代號：下市日／最後交易日）" + \
        ", ".join(f"{x['stock_id']}：{x['delisted']}／{x['last_trade']}" for x in results), {
        "samples": results, "delisting_total": len(rows)}


def finmind_revenue_timing(http: Http, token: str | None):
    status, data = finmind(http, token, dataset="TaiwanStockMonthRevenue", data_id="2330",
                           start_date="2024-01-01")
    rows = data.get("data", []) if isinstance(data, dict) else []
    lags = []
    for r in rows:
        if not r.get("create_time"):
            continue
        month_end = dt.date(r["revenue_year"], r["revenue_month"], 1)
        month_end = (month_end.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        lags.append((dt.date.fromisoformat(r["create_time"][:10]) - month_end).days)
    with_ct = [r for r in rows if r.get("create_time")]
    ok = bool(rows) and len(with_ct) == len(rows)
    summary = f"2330 共 {len(rows)} 筆，含 create_time {len(with_ct)} 筆"
    if with_ct:
        summary += f"（最早 {with_ct[0]['revenue_year']}/{with_ct[0]['revenue_month']:02d} 起）"
    if lags:
        summary += f"；月底到 create_time 天數 {min(lags)}–{max(lags)} 天"
    if not ok:
        summary += "；歷史資料缺公告時間，不能當作 point-in-time 依據"
    return ok, summary, {"lags_days": lags, "with_create_time": with_ct}


def finmind_financial_dates(http: Http, token: str | None):
    status, data = finmind(http, token, dataset="TaiwanStockFinancialStatements", data_id="2330",
                           start_date="2024-01-01")
    rows = data.get("data", []) if isinstance(data, dict) else []
    dates = sorted({r["date"] for r in rows})
    keys = sorted(rows[0].keys()) if rows else []
    has_publish = any(k for k in keys if k not in ("date", "stock_id", "type", "value", "origin_name"))
    # date 全落在季底 → 代表是「財報期間」而非公告日
    quarter_ends = all(d[5:] in ("03-31", "06-30", "09-30", "12-31") for d in dates)
    return (not quarter_ends or has_publish), (
        f"日期 {', '.join(dates)}；欄位 {keys}；"
        + ("date 皆為季底，沒有公告日欄位，需另補公告時間" if quarter_ends and not has_publish
           else "可能含公告日，需人工確認")), {"dates": dates, "fields": keys}


def finmind_quota(http: Http, token: str | None):
    if not token:
        return True, "未設定 FINMIND_TOKEN，略過（未登入上限依官網為每小時 300 次）", {}
    status, data = http.get_json("https://api.web.finmindtrade.com/v2/user_info",
                                 bucket="finmind", delay=FINMIND_DELAY_SEC, label="finmind_user_info",
                                 headers={"Authorization": f"Bearer {token}"})
    if status != 200:
        return False, f"HTTP {status}", {"response": data}
    return True, f"已用 {data.get('user_count')} / 上限 {data.get('api_request_limit')}", {
        k: data.get(k) for k in ("user_count", "api_request_limit", "level")}


# ---------------------------------------------------------------------------
# 公開資訊觀測站（MOPS）
# ---------------------------------------------------------------------------


def mops_quarterly_income(http: Http, year: int, season: int, market: str = "sii"):
    """MOPS 舊版：某季全市場綜合損益彙總（依產業分多張表）。"""
    roc_year = year - 1911
    status, text = http.get(
        "https://mopsov.twse.com.tw/mops/web/ajax_t163sb04",
        bucket="mops", delay=EXCHANGE_DELAY_SEC, label=f"mops_t163sb04_{market}_{year}Q{season}",
        data={"encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1",
              "isQuery": "Y", "TYPEK": market, "year": str(roc_year), "season": f"{season:02d}"})
    if status != 200:
        return False, f"HTTP {status}", {}
    tables = len(re.findall(r"<table[^>]*class='hasBorder'", text))
    codes = set(re.findall(r"<td[^>]*>\s*(\d{4})\s*</td>", text))
    has_deadline_note = "申報期限" in text
    ok = len(codes) > 0
    return ok, (f"{year}Q{season} {'上市' if market == 'sii' else '上櫃'}：{len(codes)} 家公司、"
                f"{tables} 張產業別表；{'有' if has_deadline_note else '無'}法定申報期限說明"), {
        "company_count": len(codes), "industry_tables": tables, "sample_codes": sorted(codes)[:10]}


# ---------------------------------------------------------------------------
# 報告
# ---------------------------------------------------------------------------


def write_report(out_dir: Path, args, results: list[CheckResult], http: Http) -> Path:
    lines = [
        "# 資料來源探測報告",
        "",
        f"- 執行時間：{dt.datetime.now().isoformat(timespec='seconds')}",
        f"- 比對日期：{args.date}　歷史日期：{args.hist_date}",
        f"- FinMind token：{'有' if os.environ.get('FINMIND_TOKEN') else '無（未登入等級）'}",
        f"- 總請求數：{len(http.request_log)}",
        "",
        "## 結果",
        "",
        "| 結果 | 來源 | 檢查項目 | 摘要 | 秒數 |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        summary = r.summary.replace("|", "／")
        lines.append(f"| {'✅' if r.ok else '⚠️'} | {r.source} | {r.name} | {summary} | {r.elapsed_sec} |")
    lines += ["", "## 細節", ""]
    for r in results:
        if r.details:
            lines += [f"### {r.name}", "", "```json",
                      json.dumps(r.details, ensure_ascii=False, indent=2)[:3000], "```", ""]
    lines += ["## 請求紀錄", "", "| # | 狀態 | 秒數 | 大小 | URL |", "|---|---|---|---|---|"]
    for q in http.request_log:
        lines.append(f"| {q['seq']} | {q['status']} | {q['elapsed_sec']} | {q['bytes']} | `{q['url']}` |")
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def last_weekday(today: dt.date) -> dt.date:
    d = today - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def main() -> int:
    parser = argparse.ArgumentParser(description="台股資料來源探測")
    parser.add_argument("--date", default=last_weekday(dt.date.today()).isoformat(),
                        help="與官方比對的交易日（預設：最近一個平日，若遇假日請手動指定）")
    parser.add_argument("--hist-date", default="2015-03-02", help="測試歷史回補的交易日")
    parser.add_argument("--mops-year", type=int, default=2024)
    parser.add_argument("--mops-season", type=int, default=1)
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(__file__).resolve().parent / "output" / run_id
    http = Http(out_dir / "raw")
    token = os.environ.get("FINMIND_TOKEN") or None

    checks = [
        ("OpenAPI 最新上市行情", "TWSE", twse_openapi_latest, http),
        ("OpenAPI 最新上櫃行情", "TPEx", tpex_openapi_latest, http),
        ("網站歷史行情回補", "TWSE/TPEx", exchange_history, http, args.hist_date),
        ("網站歷史三大法人", "TWSE", twse_t86_history, http, args.hist_date),
        ("一次取全市場股價", "FinMind", finmind_full_market, http, token, args.date),
        ("股價與官方比對", "FinMind", finmind_vs_official, http, token, args.date),
        ("下市股票歷史股價", "FinMind", finmind_delisted_history, http, token),
        ("月營收公告時間", "FinMind", finmind_revenue_timing, http, token),
        ("財報日期定義", "FinMind", finmind_financial_dates, http, token),
        ("帳號用量", "FinMind", finmind_quota, http, token),
        ("季財報彙總（上市）", "MOPS", mops_quarterly_income, http, args.mops_year, args.mops_season, "sii"),
        ("季財報彙總（上櫃）", "MOPS", mops_quarterly_income, http, args.mops_year, args.mops_season, "otc"),
    ]

    print(f"輸出目錄：{out_dir}")
    results = [run_check(name, source, fn, *fn_args) for name, source, fn, *fn_args in checks]

    (out_dir / "results.json").write_text(
        json.dumps({"args": vars(args), "results": [asdict(r) for r in results],
                    "requests": http.request_log}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    report = write_report(out_dir, args, results, http)
    print(f"\n報告：{report}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
