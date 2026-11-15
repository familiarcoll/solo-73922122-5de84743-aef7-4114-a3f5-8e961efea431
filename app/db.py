"""SQLite 持久化：以“发布批次”为最小可见单位。

* 每个 (meter_id, local_date) 至多有一个已发布批次；
* 成功重传在单事务内删除旧批次全部读数并写入新批次（完整替换）；
* 映射失败 / 跨日重复的上传不执行任何写库操作，此前发布的该日数据原样保留；
* 查询只返回同一完整批次的 UTC 读数；
* 跨日重复比对与当日发布在 **同一个 BEGIN IMMEDIATE 写事务** 内完成：
  事务期间另一实例对前日批次的重传会在获取写锁处阻塞，保证比对依据的
  前日批次与发布时刻已提交的版本一致，不会按过期版本作出决定。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .tzmap import AssignedRow, RowStatus, local_candidates

UTC = ZoneInfo("UTC")

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    meter_id      TEXT    NOT NULL,
    local_date    TEXT    NOT NULL,
    published_at  TEXT    NOT NULL,
    row_count     INTEGER NOT NULL,
    UNIQUE (meter_id, local_date)
);

CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL
        REFERENCES batches(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    local_time  TEXT    NOT NULL,
    utc_time    TEXT    NOT NULL,
    reading     REAL    NOT NULL,
    row_status  TEXT    NOT NULL,
    fold        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (batch_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_readings_batch
    ON readings(batch_id, utc_time);
"""


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def get_db_path() -> str:
    return os.environ.get("HEATMETER_DB", "/data/heatmeter.db")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    # isolation_level=None：关闭模块隐式事务，BEGIN/COMMIT 全部显式管理，
    # 以便使用 BEGIN IMMEDIATE 提前获取写锁并精确界定事务边界。
    conn = sqlite3.connect(
        path, timeout=30.0, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 锁竞争时 busy handler 等待 30s，配合 BEGIN IMMEDIATE 串行化写事务
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _backfill_fold(conn: sqlite3.Connection) -> None:
    """为旧库（无 fold 列）补列并回填历史读数的回拨档位。

    普通钟点恒为 0；回拨行按其已发布 UTC 命中的候选下标确定
    （候选升序，下标即 fold 档位）。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(readings)")}
    if "fold" in cols:
        return
    conn.execute("ALTER TABLE readings ADD COLUMN fold INTEGER NOT NULL DEFAULT 0")
    rows = conn.execute(
        "SELECT id, local_time, utc_time, row_status FROM readings"
    ).fetchall()
    for r in rows:
        if r["row_status"] != RowStatus.AMBIGUOUS.value:
            continue
        local_naive = datetime.fromisoformat(r["local_time"])
        _, candidates = local_candidates(local_naive)
        utc = datetime.fromisoformat(r["utc_time"])
        fold = 0
        for idx, cand in enumerate(candidates):
            if cand == utc:
                fold = idx
                break
        conn.execute("UPDATE readings SET fold = ? WHERE id = ?", (fold, r["id"]))


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _backfill_fold(conn)
    conn.commit()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """开启立即写事务（BEGIN IMMEDIATE），提交/回滚边界明确。

    进入即获取 RESERVED 写锁：事务内读到的任何已提交批次在 COMMIT 前
    都不会被其他连接替换，保证“比对依据”与“发布动作”基于同一版本。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def insert_batch_locked(
    conn: sqlite3.Connection,
    meter_id: str,
    local_date: date,
    assigned: list[AssignedRow],
) -> dict[str, Any]:
    """调用方必须已持有写事务；单事务完整替换该 (meter, date) 批次。"""
    date_str = local_date.isoformat()
    published_at = _utc_now_iso()

    old = conn.execute(
        "SELECT id, published_at, row_count FROM batches "
        "WHERE meter_id = ? AND local_date = ?",
        (meter_id, date_str),
    ).fetchone()

    # 先整体移除旧批次（UNIQUE(meter_id, local_date) 要求旧行先消失）
    if old is not None:
        conn.execute("DELETE FROM readings WHERE batch_id = ?", (old["id"],))
        conn.execute("DELETE FROM batches WHERE id = ?", (old["id"],))

    cur = conn.execute(
        "INSERT INTO batches (meter_id, local_date, published_at, row_count) "
        "VALUES (?, ?, ?, ?)",
        (meter_id, date_str, published_at, len(assigned)),
    )
    new_batch_id = cur.lastrowid

    conn.executemany(
        "INSERT INTO readings (batch_id, seq, local_time, utc_time, reading, "
        "row_status, fold) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                new_batch_id,
                r.seq,
                r.local_time.isoformat(),
                r.utc_time.isoformat(),
                r.reading,
                r.status.value,
                r.fold,
            )
            for r in assigned
        ],
    )

    return {
        "batch_id": new_batch_id,
        "replaced_batch_id": old["id"] if old else None,
        "previous_published_at": old["published_at"] if old else None,
        "published_at": published_at,
        "row_count": len(assigned),
    }


def publish_batch(
    conn: sqlite3.Connection,
    meter_id: str,
    local_date: date,
    assigned: list[AssignedRow],
) -> dict[str, Any]:
    """独立发布入口：自行开启 BEGIN IMMEDIATE 事务并完整替换当日批次。"""
    with immediate_transaction(conn):
        return insert_batch_locked(conn, meter_id, local_date, assigned)


def get_published_batch(
    conn: sqlite3.Connection, meter_id: str, local_date: date
) -> dict[str, Any] | None:
    """返回该日已发布批次及其全部读数；无发布返回 None。"""
    batch = conn.execute(
        "SELECT id, meter_id, local_date, published_at, row_count "
        "FROM batches WHERE meter_id = ? AND local_date = ?",
        (meter_id, local_date.isoformat()),
    ).fetchone()
    if batch is None:
        return None

    rows = conn.execute(
        "SELECT seq, local_time, utc_time, reading, row_status, fold "
        "FROM readings WHERE batch_id = ? ORDER BY utc_time ASC, seq ASC",
        (batch["id"],),
    ).fetchall()

    return {
        "batch_id": batch["id"],
        "meter_id": batch["meter_id"],
        "local_date": batch["local_date"],
        "published_at": batch["published_at"],
        "row_count": batch["row_count"],
        "readings": [dict(r) for r in rows],
    }


def list_published_dates(conn: sqlite3.Connection, meter_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, local_date, published_at, row_count FROM batches "
        "WHERE meter_id = ? ORDER BY local_date ASC",
        (meter_id,),
    ).fetchall()
    return [dict(r) for r in rows]
