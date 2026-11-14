"""限速、可重試的 HTTP 客戶端（只用標準函式庫）。"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (apex personal research)"
RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, delay_sec: float = 3.0, max_retries: int = 3, timeout_sec: float = 60.0,
                 backoff_base_sec: float = 5.0):
        self.delay_sec = delay_sec
        self.max_retries = max_retries
        self.timeout_sec = timeout_sec
        self.backoff_base_sec = backoff_base_sec
        self._last_call: dict[str, float] = {}

    def _throttle(self, host: str) -> None:
        last = self._last_call.get(host)
        if last is not None:
            wait = self.delay_sec - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[host] = time.monotonic()

    def get_text(self, url: str, params: dict[str, str] | None = None) -> str:
        full_url = url + ("?" + urllib.parse.urlencode(params) if params else "")
        host = urllib.parse.urlsplit(full_url).netloc
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                backoff = self.backoff_base_sec * (3 ** (attempt - 1))
                log.warning("重試 %s（第 %d 次，等待 %.0f 秒）：%s", full_url, attempt, backoff, last_error)
                time.sleep(backoff)
            self._throttle(host)
            req = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code not in RETRY_STATUS:
                    raise FetchError(f"HTTP {e.code}: {full_url}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_error = e
        raise FetchError(f"重試 {self.max_retries} 次仍失敗：{full_url}（{last_error}）")
