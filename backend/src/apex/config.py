"""路徑與執行設定。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]  # backend/
REPO_ROOT = BACKEND_ROOT.parent  # data/、personal/、frontend/ 在 repo 根目錄


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

    @property
    def personal_dir(self) -> Path:
        """持股、交易紀錄、每日報告：只存本機（.gitignore 排除）。"""
        return Path(os.environ.get("APEX_PERSONAL_DIR", REPO_ROOT / "personal"))

    @property
    def trades_path(self) -> Path:
        return self.personal_dir / "trades.csv"

    @property
    def commitments_path(self) -> Path:
        """模擬追蹤訊號的雜湊承諾（公開、進版本控制）。"""
        return BACKEND_ROOT / "paper" / "commitments.csv"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("APEX_DATA_DIR", REPO_ROOT / "data"))
    delay = float(os.environ.get("APEX_REQUEST_DELAY_SEC", "3.0"))
    return Settings(data_dir=data_dir, request_delay_sec=delay)
