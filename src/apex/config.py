"""路徑與執行設定。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    request_delay_sec: float = 3.0
    max_retries: int = 3
    timeout_sec: float = 60.0

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "apex.duckdb"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("APEX_DATA_DIR", REPO_ROOT / "data"))
    delay = float(os.environ.get("APEX_REQUEST_DELAY_SEC", "3.0"))
    return Settings(data_dir=data_dir, request_delay_sec=delay)
