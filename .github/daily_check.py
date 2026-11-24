"""每日更新要不要跑：看已發布網站的 data.json，最近一個平日 18:00 之後已產生且資料齊全就跳過。

輸出到 GITHUB_OUTPUT：run（要不要跑）、final（台灣時間 06:00～08:59，開盤前最後機會）。
讀不到網站時一律照跑。國定假日也適用：看的是產生時間與「是否為應有的資料日」，不是日期本身。
"""

import datetime as dt
import json
import os
import sys
import time
import urllib.request

TW = dt.timezone(dt.timedelta(hours=8))


def boundary(now: dt.datetime) -> dt.datetime:
    """現在（含）以前最近一個平日的 18:00。"""
    b = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if b > now:
        b -= dt.timedelta(days=1)
    while b.weekday() >= 5:
        b -= dt.timedelta(days=1)
    return b


def up_to_date(data: dict, now: dt.datetime) -> bool:
    generated = dt.datetime.fromisoformat(data["generated"])
    return (generated >= boundary(now) and not data["stale"] and bool(data["status"])
            and all(x["ok"] for x in data["status"]))


def decide(event: str, data: dict | None, now: dt.datetime) -> bool:
    if event != "schedule" or data is None:
        return True
    try:
        return not up_to_date(data, now)
    except (KeyError, TypeError, ValueError):
        return True


def fetch(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{url}?t={int(time.time())}", timeout=20) as r:
            return json.load(r)
    except (OSError, ValueError) as e:
        print(f"讀不到網站資料，照常執行：{e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    now = dt.datetime.now(TW).replace(tzinfo=None)
    event = os.environ.get("EVENT", "")
    data = fetch(sys.argv[1])
    run, final = decide(event, data, now), event == "schedule" and 6 <= now.hour < 9
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write(f"run={str(run).lower()}\nfinal={str(final).lower()}\n")
    print(f"::notice title=觸發紀錄::{event}、排程 {os.environ.get('CRON') or '無（手動）'}、"
          f"台灣時間 {now:%Y-%m-%d %H:%M:%S}、網站資料日 {(data or {}).get('data_date')}、"
          f"{'執行更新' if run else '已是最新，跳過'}")
