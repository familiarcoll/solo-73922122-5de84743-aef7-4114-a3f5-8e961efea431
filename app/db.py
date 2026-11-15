"""SQLite 持久化：以“发布批次”为最小可见单位。

* 每个 (meter_id, local_date) 至多有一个已发布批次；
* 成功重传在单事务内删除旧批次全部读数并写入新批次（完整替换）；
* 映射失败的上传不执行任何写库操作，此前发布的该日数据原样保留；
* 查询只返回同一完整批次的 UTC 读数。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .tzmap import AssignedRow

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
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def publish_batch(
    conn: sqlite3.Connection,
    meter_id: str,
    local_date: date,
    assigned: list[AssignedRow],
) -> dict[str, Any]:
    """单事务完整替换该 (meter, date) 的已发布批次，返回新旧批次信息。"""
    date_str = local_date.isoformat()
    published_at = _utc_now_iso()

    try:
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
            "INSERT INTO readings (batch_id, seq, local_time, utc_time, reading, row_status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    new_batch_id,
                    r.seq,
                    r.local_time.isoformat(),
                    r.utc_time.isoformat(),
                    r.reading,
                    r.status.value,
                )
                for r in assigned
            ],
        )

        if old is not None:
            # 先删读数再删旧批次（外键级联也会删，显式删除更清晰）
            conn.execute("DELETE FROM readings WHERE batch_id = ?", (old["id"],))
            conn.execute("DELETE FROM batches WHERE id = ?", (old["id"],))

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "batch_id": new_batch_id,
        "replaced_batch_id": old["id"] if old else None,
        "previous_published_at": old["published_at"] if old else None,
        "published_at": published_at,
        "row_count": len(assigned),
    }


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
        "SELECT seq, local_time, utc_time, reading, row_status "
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
