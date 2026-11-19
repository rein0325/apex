"""原始回應存檔：data/raw/<source>/<year>/<key>.json。

規格書要求資料可追溯；保存原始回應也讓解析程式修正後可以不重抓、直接重新解析。
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class RawStore:
    def __init__(self, root: Path):
        self.root = root

    def path(self, source: str, key: str) -> Path:
        return self.root / source / key[:4] / f"{key}.json"

    def exists(self, source: str, key: str) -> bool:
        return self.path(source, key).exists()

    def load(self, source: str, key: str) -> str:
        return self.path(source, key).read_text(encoding="utf-8")

    def save(self, source: str, key: str, text: str) -> Path:
        path = self.path(source, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return path

    def delete(self, source: str, key: str) -> None:
        self.path(source, key).unlink(missing_ok=True)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
