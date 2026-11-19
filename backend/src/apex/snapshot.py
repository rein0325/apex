"""資料庫加密快照（GitHub Actions 每日更新用，docs/網站與每日自動化.md §3）。

匯出：DuckDB EXPORT DATABASE（Parquet＋zstd，約 230 MB）→ tar → openssl AES-256（PBKDF2）加密。
還原：解密 → 解 tar → IMPORT DATABASE。

金鑰只從環境變數 APEX_DB_KEY 讀取（GitHub Secrets），不出現在指令列或紀錄中。
repo 是公開的，快照雖然放在公開的 Release 與 Actions 快取，但沒有金鑰無法讀取。
資料庫含 FinMind 等第三方資料，未加密不得公開散布。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import duckdb

KEY_ENV = "APEX_DB_KEY"
OPENSSL = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-pass", f"env:{KEY_ENV}"]


class SnapshotError(RuntimeError):
    pass


def _check_key() -> None:
    if not os.environ.get(KEY_ENV):
        raise SnapshotError(f"沒有設定環境變數 {KEY_ENV}（資料庫加密金鑰）")


def export(db_path: Path, out_file: Path) -> int:
    """把資料庫匯出成加密快照，回傳檔案大小（位元組）。"""
    _check_key()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "db"
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            con.execute(f"EXPORT DATABASE '{dump.as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
        finally:
            con.close()
        tar = Path(tmp) / "db.tar"
        with tarfile.open(tar, "w") as tf:
            tf.add(dump, arcname="db")
        subprocess.run([*OPENSSL, "-e", "-in", str(tar), "-out", str(out_file)], check=True)
    return out_file.stat().st_size


def restore(in_file: Path, db_path: Path) -> None:
    """解密快照並重建資料庫（覆蓋 db_path）。"""
    _check_key()
    if not in_file.exists():
        raise SnapshotError(f"找不到快照 {in_file}")
    with tempfile.TemporaryDirectory() as tmp:
        tar = Path(tmp) / "db.tar"
        r = subprocess.run([*OPENSSL, "-d", "-in", str(in_file), "-out", str(tar)], capture_output=True)
        if r.returncode != 0:
            raise SnapshotError("解密失敗：金鑰錯誤或檔案損壞")
        with tarfile.open(tar) as tf:
            tf.extractall(tmp, filter="data")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        new = db_path.with_suffix(".importing")
        for p in (new, Path(str(new) + ".wal")):
            p.unlink(missing_ok=True)
        con = duckdb.connect(str(new))
        try:
            con.execute(f"IMPORT DATABASE '{(Path(tmp) / 'db').as_posix()}'")
        finally:
            con.close()
        for p in (db_path, Path(str(db_path) + ".wal")):
            p.unlink(missing_ok=True)
        shutil.move(str(new), str(db_path))
