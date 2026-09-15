"""SQLite 不可变预审版本存储。

一个 input_hash 对应且仅对应一行记录；重复提交相同输入直接复用首次结果，
保证同一版本重复计算结果一致。任何已写入行都不会被更新或删除。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS preevaluation_versions (
    version_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    input_hash  TEXT NOT NULL UNIQUE,
    payload     TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


class VersionStore:
    def __init__(self, path: Optional[str | os.PathLike[str]] = None) -> None:
        path = path or os.environ.get("PREEVAL_DB_PATH", "data/preeval.db")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def get_by_hash(self, input_hash: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT version_id, result, created_at FROM "
                "preevaluation_versions WHERE input_hash = ?",
                (input_hash,),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row["result"])
        result["version_id"] = row["version_id"]
        return {
            "version_id": row["version_id"],
            "created_at": row["created_at"],
            "result": result,
        }

    def get_by_version(self, version_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT input_hash, result, created_at FROM "
                "preevaluation_versions WHERE version_id = ?",
                (version_id,),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row["result"])
        result["version_id"] = version_id
        return {
            "input_hash": row["input_hash"],
            "created_at": row["created_at"],
            "result": result,
        }

    def put_if_absent(self, input_hash: str, payload_json: str,
                      result_json: str, created_at: str) -> tuple[int, bool]:
        """幂等写入。返回 (version_id, created)；已存在时 created=False。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT version_id FROM preevaluation_versions "
                "WHERE input_hash = ?", (input_hash,),
            ).fetchone()
            if row is not None:
                return row["version_id"], False
            cur = self._conn.execute(
                "INSERT INTO preevaluation_versions "
                "(input_hash, payload, result, created_at) VALUES (?,?,?,?)",
                (input_hash, payload_json, result_json, created_at),
            )
            return int(cur.lastrowid), True

    def close(self) -> None:
        with self._lock:
            self._conn.close()
