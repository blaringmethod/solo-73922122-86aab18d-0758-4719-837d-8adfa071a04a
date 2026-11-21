"""SQLite 连接管理与建表。

写入统一走 :func:`write_transaction`：进程内互斥锁 + ``BEGIN IMMEDIATE``，
保证两个改单包并行提交时也被串行化，同一张旧单最多被替换一次。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

DEFAULT_DB_PATH = os.environ.get(
    "WAYBILL_DB_PATH", os.path.join(os.getcwd(), "data", "waybills.db")
)

# 进程内写锁：FastAPI 同步端点跑在线程池中，需要它把写事务完全串行化。
_write_lock = threading.RLock()


class WriteConflict(Exception):
    """写事务在等待期内未能取得写锁（其他进程占用）。"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS waybills (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    waybill_no         TEXT NOT NULL UNIQUE,
    status             TEXT NOT NULL CHECK (status IN ('active', 'replaced')),
    source_package_no  TEXT,
    created_at         TEXT NOT NULL,
    replaced_at        TEXT,
    replaced_by_pkg    TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    waybill_id INTEGER NOT NULL REFERENCES waybills(id),
    batch_no   TEXT NOT NULL,
    weight_kg  INTEGER NOT NULL CHECK (weight_kg > 0),
    UNIQUE (waybill_id, batch_no)
);

CREATE TABLE IF NOT EXISTS packages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    package_no  TEXT NOT NULL UNIQUE,
    payload     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('applied', 'suspended', 'rejected')),
    reason      TEXT,
    -- 处理该包时的校验逻辑版本；1 = 旧版（按单张旧单覆盖，未做跨单合计）
    logic_version INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def get_db_path() -> str:
    return os.environ.get("WAYBILL_DB_PATH", DEFAULT_DB_PATH)


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # WAL 是持久化设置；并发连接短暂占锁时沿用库中既有模式即可。
        pass
    return conn


def init_db(db_path: str | None = None) -> None:
    """建表并做幂等迁移（旧库缺少 logic_version 列时补上）。"""
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        columns = {
            r["name"] for r in conn.execute("PRAGMA table_info(packages)")
        }
        if "logic_version" not in columns:
            try:
                conn.execute(
                    "ALTER TABLE packages ADD COLUMN logic_version INTEGER "
                    "NOT NULL DEFAULT 1"
                )
            except sqlite3.OperationalError:
                # 另一个进程可能刚完成同一迁移；确认列已存在即可。
                columns = {
                    r["name"]
                    for r in conn.execute("PRAGMA table_info(packages)")
                }
                if "logic_version" not in columns:
                    raise
        conn.commit()
    finally:
        conn.close()


@contextmanager
def read_connection(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """串行化的写事务：先拿进程锁，再以 IMMEDIATE 立即获取写锁。"""
    with _write_lock:
        conn = _connect(db_path)
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise WriteConflict(f"写入繁忙，请重试: {exc}") from exc
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
