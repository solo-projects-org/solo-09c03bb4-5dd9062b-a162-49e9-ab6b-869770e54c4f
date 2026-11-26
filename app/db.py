"""SQLite 连接与表结构。

表 segment：一段连续采样，samples 为 float64 小端 BLOB。
表 window_spectrum：每个有效窗（256 点齐全）的预计算单边均方谱，
能量查询只允许访问这张表，不再回读 segment.samples。
"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS segment (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    start_sample INTEGER NOT NULL,
    n_samples    INTEGER NOT NULL,
    fs           INTEGER NOT NULL DEFAULT 256,
    name         TEXT,
    source_file  TEXT,
    samples      BLOB    NOT NULL,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    CHECK (start_sample >= 0),
    CHECK (n_samples > 0),
    CHECK (fs = 256)
);

CREATE TABLE IF NOT EXISTS window_spectrum (
    window_start INTEGER PRIMARY KEY,
    n_samples    INTEGER NOT NULL,
    fs           INTEGER NOT NULL,
    n_bins       INTEGER NOT NULL,
    power        BLOB    NOT NULL,
    total_ms     REAL    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_window_start ON window_spectrum(window_start);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开一个配置好 row_factory / 外键 / WAL 的连接。"""
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        # 某些受限文件系统不支持 WAL，退回默认回滚日志也能保证事务。
        pass
    return conn


def init_db(db_path: str) -> None:
    """幂等建表。"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
