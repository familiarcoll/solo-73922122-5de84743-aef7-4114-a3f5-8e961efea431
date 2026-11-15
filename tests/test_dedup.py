"""跨日重传检测：dedup 单元测试 + 上传接口端到端 + 并发锁/旧库迁移。"""

from __future__ import annotations

import io
import os
import sqlite3
import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import (
    connect,
    get_published_batch,
    immediate_transaction,
    init_db,
)
from app.dedup import (
    MIN_DISTINCT_READINGS,
    MIN_MATCH_RUN,
    DayReading,
    current_day_readings,
    detect_reupload,
    find_duplicate_runs,
    previous_day_readings,
)
from app.tzmap import AssignedRow, RowStatus, solve_batch
from datetime import datetime


# ================================================================ dedup 单元


def _dr(seq: int, clock: str, fold: int, reading: float) -> DayReading:
    return DayReading(seq=seq, clock=clock, fold=fold, reading=reading)


def test_dedup_five_consecutive_matches_detected():
    current = [_dr(i, f"00:0{i}", 0, 100.0 + i) for i in range(5)]
    previous = [_dr(i + 10, f"00:0{i}", 0, 100.0 + i) for i in range(5)]
    run = detect_reupload(current, previous)
    assert run is not None
    assert run.length == 5
    assert run.distinct_readings == 5
    assert run.current_seqs == [0, 1, 2, 3, 4]
    assert run.previous_seqs == [10, 11, 12, 13, 14]


def test_dedup_four_consecutive_below_threshold_passes():
    current = [_dr(i + 1, f"00:0{i}", 0, 100.0 + i) for i in range(4)]
    previous = [_dr(i + 1, f"00:0{i}", 0, 100.0 + i) for i in range(4)]
    assert detect_reupload(current, previous) is None


def test_dedup_constant_readings_not_rejected():
    # 连续 6 条完全同钟点同档位，但读数恒定（不同读数 < 3）→ 视为表停走，放行
    current = [_dr(i + 1, f"00:0{i}", 0, 5.0) for i in range(6)]
    previous = [_dr(i + 1, f"00:0{i}", 0, 5.0) for i in range(6)]
    assert detect_reupload(current, previous) is None
    # 仅 2 个不同读数同样不达标
    current2 = [_dr(i + 1, f"00:0{i}", 0, 1.0 if i % 2 else 2.0) for i in range(6)]
    previous2 = list(current2)
    assert detect_reupload(current2, previous2) is None


def test_dedup_fold_mismatch_breaks_matching():
    # 钟点与读数相同但 fold 档位不同 → 不构成匹配
    current = [_dr(i + 1, "01:00", 0, 10.0 + i) for i in range(5)]
    previous = [_dr(i + 1, "01:00", 1, 10.0 + i) for i in range(5)]
    assert detect_reupload(current, previous) is None


def test_dedup_non_consecutive_matches_do_not_count():
    # 3 条匹配 → 1 条打断 → 再 3 条匹配：两段都不足 5
    current = [_dr(i + 1, f"00:0{i}", 0, 10.0 + i) for i in range(7)]
    previous = [
        _dr(1, "00:00", 0, 10.0),
        _dr(2, "00:01", 0, 11.0),
        _dr(3, "00:02", 0, 12.0),
        _dr(4, "00:02", 0, 99.0),  # 读数不同，打断
        _dr(5, "00:04", 0, 14.0),
        _dr(6, "00:05", 0, 15.0),
        _dr(7, "00:06", 0, 16.0),
    ]
    assert detect_reupload(current, previous) is None


def test_dedup_longer_run_with_surrounding_noise():
    # 前后各有不匹配噪声，中间 5 条连续命中
    current = (
        [_dr(1, "23:00", 0, 9.0)]
        + [_dr(i + 2, f"00:0{i}", 0, 20.0 + i) for i in range(5)]
        + [_dr(7, "23:59", 0, 8.0)]
    )
    previous = (
        [_dr(90, "22:00", 0, 1.0)]
        + [_dr(i + 91, f"00:0{i}", 0, 20.0 + i) for i in range(5)]
        + [_dr(96, "23:58", 0, 2.0)]
    )
    run = detect_reupload(current, previous)
    assert run is not None
    assert run.length == 5
    assert run.current_seqs == [2, 3, 4, 5, 6]


def test_dedup_normal_fold_zero_matches_ambiguous_fold_zero():
    # 普通钟点 fold=0 与回拨日第一次经过（ambiguous, fold=0）同键即匹配
    current = [_dr(i + 1, f"01:0{i}", 0, 30.0 + i) for i in range(5)]
    previous = [
        DayReading(seq=i + 1, clock=f"01:0{i}", fold=0, reading=30.0 + i)
        for i in range(5)
    ]
    run = find_duplicate_runs(current, previous)
    assert len(run) == 1
    assert run[0].distinct_readings == 5


def test_dedup_threshold_constants():
    assert MIN_MATCH_RUN == 5
    assert MIN_DISTINCT_READINGS == 3


def test_day_reading_builders():
    # 当日：AssignedRow -> DayReading，fold 透传、钟点只取 HH:MM
    assigned = [
        AssignedRow(
            seq=1,
            local_time=datetime(2026, 11, 1, 1, 30),
            reading=1.5,
            status=RowStatus.AMBIGUOUS,
            utc_time=datetime(2026, 11, 1, 6, 30),
            fold=1,
        )
    ]
    cur = current_day_readings(assigned)
    assert cur == [DayReading(seq=1, clock="01:30", fold=1, reading=1.5)]

    # 前日：库行（naive ISO local_time + fold）-> DayReading
    prev = previous_day_readings(
        [
            {
                "seq": 7,
                "local_time": "2026-10-31T00:05:00",
                "reading": 2.5,
                "fold": 0,
            }
        ]
    )
    assert prev == [DayReading(seq=7, clock="00:05", fold=0, reading=2.5)]


# ================================================================ API 端到端


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("HEATMETER_DB", str(db_file))
    from app.main import app

    with TestClient(app) as c:
        yield c


def _upload(client, meter: str, d: date, csv_text: str):
    return client.post(
        f"/meters/{meter}/dates/{d.isoformat()}/batches",
        files={"file": ("readings.csv", io.BytesIO(csv_text.encode()), "text/csv")},
    )


_NORMAL_CSV = (
    "seq,local_time,reading\n"
    "1,00:00,100.0\n"
    "2,00:01,101.5\n"
    "3,00:02,102.25\n"
    "4,00:03,103.75\n"
    "5,00:04,104.5\n"
    "6,00:05,105.0\n"
)


def test_duplicate_reupload_next_day_rejected(client):
    meter = "DUP-1"
    d1, d2 = date(2026, 6, 15), date(2026, 6, 16)

    r1 = _upload(client, meter, d1, _NORMAL_CSV)
    assert r1.status_code == 200, r1.text
    prev_batch_id = r1.json()["batch_id"]

    # 同一份 CSV 换日期再传：UTC 映射合法，但跨日重传命中
    r2 = _upload(client, meter, d2, _NORMAL_CSV)
    assert r2.status_code == 422, r2.text
    detail = r2.json()["detail"]
    assert detail["error_code"] == "DUPLICATE_OF_PREVIOUS_DAY_BATCH"
    assert detail["meter_id"] == meter
    assert detail["local_date"] == d2.isoformat()
    assert detail["previous_local_date"] == d1.isoformat()
    assert detail["previous_batch_id"] == prev_batch_id
    assert detail["matched_run_length"] == 6
    assert detail["matched_distinct_readings"] == 6
    assert detail["involved_seqs"] == [1, 2, 3, 4, 5, 6]
    assert detail["previous_involved_seqs"] == [1, 2, 3, 4, 5, 6]
    # 当日此前没有批次
    assert detail["retained_previous_batch"] is False

    # 被拒绝则当日无任何数据，前日批次完好
    assert client.get(f"/meters/{meter}/dates/{d2.isoformat()}/readings").status_code == 404
    q = client.get(f"/meters/{meter}/dates/{d1.isoformat()}/readings").json()
    assert q["batch_id"] == prev_batch_id
    assert q["row_count"] == 6
    listing = client.get(f"/meters/{meter}/dates").json()["batches"]
    assert [b["local_date"] for b in listing] == [d1.isoformat()]


def test_no_previous_day_batch_falls_back_to_original_rule(client):
    # 没有前一日本地批次：即使内容“像重复”也按原规则直接发布
    meter = "DUP-2"
    d = date(2026, 6, 16)  # 2026-06-15 无批次
    r = _upload(client, meter, d, _NORMAL_CSV)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"


def test_only_immediately_previous_local_day_is_compared(client):
    meter = "DUP-3"
    # 两天前有批次，前一天没有 → 不参与比对，正常发布
    _upload(client, meter, date(2026, 6, 14), _NORMAL_CSV)
    r = _upload(client, meter, date(2026, 6, 16), _NORMAL_CSV)
    assert r.status_code == 200, r.text


def test_duplicate_rejection_retains_current_day_existing_batch(client):
    meter = "DUP-4"
    d1, d2 = date(2026, 6, 15), date(2026, 6, 16)
    _upload(client, meter, d1, _NORMAL_CSV)

    # 当日先发布一版不同数据
    current_csv = (
        "seq,local_time,reading\n"
        "1,08:00,1.0\n2,08:01,2.0\n3,08:02,3.0\n"
    )
    pub = _upload(client, meter, d2, current_csv)
    assert pub.status_code == 200
    current_batch_id = pub.json()["batch_id"]

    # 再传与前日重复的批次：拒绝，且当日原批次保留
    r = _upload(client, meter, d2, _NORMAL_CSV)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error_code"] == "DUPLICATE_OF_PREVIOUS_DAY_BATCH"
    assert detail["retained_previous_batch"] is True
    assert detail["retained_batch"]["batch_id"] == current_batch_id

    q = client.get(f"/meters/{meter}/dates/{d2.isoformat()}/readings").json()
    assert q["batch_id"] == current_batch_id
    assert [x["reading"] for x in q["readings"]] == [1.0, 2.0, 3.0]


def test_same_clock_different_readings_publishes(client):
    meter = "DUP-5"
    d1, d2 = date(2026, 6, 15), date(2026, 6, 16)
    _upload(client, meter, d1, _NORMAL_CSV)
    changed = (
        "seq,local_time,reading\n"
        "1,00:00,500.0\n"
        "2,00:01,501.5\n"
        "3,00:02,502.25\n"
        "4,00:03,503.75\n"
        "5,00:04,504.5\n"
        "6,00:05,505.0\n"
    )
    r = _upload(client, meter, d2, changed)
    assert r.status_code == 200, r.text


def test_dedup_isolated_per_meter(client):
    # 不同表号之间互不影响
    _upload(client, "DUP-A", date(2026, 6, 15), _NORMAL_CSV)
    r = _upload(client, "DUP-B", date(2026, 6, 16), _NORMAL_CSV)
    assert r.status_code == 200, r.text


def test_fold_distinction_blocks_or_allows_matching(client):
    """回拨档位参与比对：前日 fold=1 行不会被当日普通行（fold=0）匹配。"""
    meter = "DUP-6"
    fold_day, normal_day = date(2026, 11, 1), date(2026, 11, 2)

    # 前日：秋季回拨日，01 点两次经过，映射唯一
    previous_csv = (
        "seq,local_time,reading\n"
        "1,00:00,100\n"
        "2,00:01,101\n"
        "3,00:02,102\n"
        "4,00:03,103\n"
        "5,00:04,104\n"
        "6,01:00,110\n"   # ambiguous fold=0
        "7,01:59,111\n"   # ambiguous fold=0
        "8,01:00,112\n"   # ambiguous fold=1
        "9,01:59,113\n"   # ambiguous fold=1
        "10,02:00,120\n"
    )
    rp = _upload(client, meter, fold_day, previous_csv)
    assert rp.status_code == 200, rp.text
    prev_rows = client.get(
        f"/meters/{meter}/dates/{fold_day.isoformat()}/readings"
    ).json()["readings"]
    fold_by_clock = {(r["local_time"][-8:-3], r["seq"]): r["fold"] for r in prev_rows}
    assert fold_by_clock[("01:00", 6)] == 0
    assert fold_by_clock[("01:00", 8)] == 1

    # 当日 A：复用前日 fold=0 的 00:00–00:04 五连（普通日 fold=0）→ 拒绝
    dup_csv = (
        "seq,local_time,reading\n"
        "1,00:00,100\n2,00:01,101\n3,00:02,102\n"
        "4,00:03,103\n5,00:04,104\n"
    )
    ra = _upload(client, meter, normal_day, dup_csv)
    assert ra.status_code == 422
    assert ra.json()["detail"]["error_code"] == "DUPLICATE_OF_PREVIOUS_DAY_BATCH"
    assert ra.json()["detail"]["involved_seqs"] == [1, 2, 3, 4, 5]

    # 当日 B：复用前日 fold=1 行的钟点/读数，但当日为普通钟点 fold=0 →
    # 档位不同不匹配，其余行也不构成 5 连 → 放行
    fold1_csv = (
        "seq,local_time,reading\n"
        "1,01:00,112\n2,01:59,113\n"
        "3,03:00,200\n4,03:01,201\n5,03:02,202\n"
    )
    rb = _upload(client, meter, normal_day, fold1_csv)
    assert rb.status_code == 200, rb.text


def test_previous_day_reupload_changes_comparison_basis(client):
    """前日批次被重传替换后，比对基于新的已提交版本。"""
    meter = "DUP-7"
    d1, d2 = date(2026, 6, 15), date(2026, 6, 16)
    r1 = _upload(client, meter, d1, _NORMAL_CSV)
    assert r1.status_code == 200
    old_prev_id = r1.json()["batch_id"]

    # 先确认旧版本下确实会被拒
    assert _upload(client, meter, d2, _NORMAL_CSV).status_code == 422

    # 另一实例重传前日：内容改成完全不同的数据并完整替换
    replaced_csv = (
        "seq,local_time,reading\n"
        "1,09:00,900.0\n2,09:01,901.0\n3,09:02,902.0\n"
    )
    rr = _upload(client, meter, d1, replaced_csv)
    assert rr.status_code == 200
    assert rr.json()["replaced_batch_id"] == old_prev_id
    new_prev_id = rr.json()["batch_id"]

    # 同样的当日上传现在基于新前日版本判定 → 发布
    r2 = _upload(client, meter, d2, _NORMAL_CSV)
    assert r2.status_code == 200, r2.text

    q1 = client.get(f"/meters/{meter}/dates/{d1.isoformat()}/readings").json()
    assert q1["batch_id"] == new_prev_id
    q2 = client.get(f"/meters/{meter}/dates/{d2.isoformat()}/readings").json()
    assert q2["row_count"] == 6


def test_concurrent_previous_day_reupload_never_uses_stale_snapshot(client):
    """写事务串行化：当日发布必须等待前日重传提交，并基于同一已提交版本决策。"""
    meter = "DUP-8"
    d1, d2 = date(2026, 6, 15), date(2026, 6, 16)
    r1 = _upload(client, meter, d1, _NORMAL_CSV)
    old_prev_id = r1.json()["batch_id"]

    acquired = threading.Event()
    db_path = os.environ["HEATMETER_DB"]

    def replace_previous_day_under_lock():
        # 模拟另一实例：先拿到写锁并保持，再提交“不同内容”的前日重传
        conn = connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            acquired.set()
            time.sleep(1.0)
            conn.execute("DELETE FROM readings WHERE batch_id = ?", (old_prev_id,))
            conn.execute("DELETE FROM batches WHERE id = ?", (old_prev_id,))
            cur = conn.execute(
                "INSERT INTO batches (meter_id, local_date, published_at, row_count) "
                "VALUES (?, ?, ?, ?)",
                (meter, d1.isoformat(), "2026-06-15T10:00:00+00:00", 3),
            )
            new_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO readings (batch_id, seq, local_time, utc_time, reading, "
                "row_status, fold) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (new_id, 1, "2026-06-15T09:00:00", "2026-06-15T13:00:00+00:00", 900.0, "normal", 0),
                    (new_id, 2, "2026-06-15T09:01:00", "2026-06-15T13:01:00+00:00", 901.0, "normal", 0),
                    (new_id, 3, "2026-06-15T09:02:00", "2026-06-15T13:02:00+00:00", 902.0, "normal", 0),
                ],
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    t = threading.Thread(target=replace_previous_day_under_lock)
    t.start()
    assert acquired.wait(5), "后台线程未能获取写锁"

    start = time.monotonic()
    r = _upload(client, meter, d2, _NORMAL_CSV)
    elapsed = time.monotonic() - start
    t.join(timeout=10)

    # 当日上传在锁上等待，且依据提交后的新前日版本放行（而非按旧版本拒绝）
    assert r.status_code == 200, r.text
    assert elapsed >= 0.8, f"上传未等待写锁即返回（elapsed={elapsed:.2f}s）"

    q1 = client.get(f"/meters/{meter}/dates/{d1.isoformat()}/readings").json()
    assert q1["row_count"] == 3
    assert q1["batch_id"] != old_prev_id
    q2 = client.get(f"/meters/{meter}/dates/{d2.isoformat()}/readings").json()
    assert q2["row_count"] == 6


def test_legacy_db_without_fold_column_is_migrated(tmp_path, monkeypatch):
    """旧库 readings 无 fold 列：init_db 自动补列并按 UTC 候选回填档位。"""
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("HEATMETER_DB", str(db_path))

    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meter_id TEXT NOT NULL, local_date TEXT NOT NULL,
            published_at TEXT NOT NULL, row_count INTEGER NOT NULL,
            UNIQUE (meter_id, local_date)
        );
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,
            seq INTEGER NOT NULL, local_time TEXT NOT NULL, utc_time TEXT NOT NULL,
            reading REAL NOT NULL, row_status TEXT NOT NULL,
            UNIQUE (batch_id, seq)
        );
        """
    )
    legacy.execute(
        "INSERT INTO batches (meter_id, local_date, published_at, row_count) "
        "VALUES ('LEG-1', '2026-11-01', '2026-11-02T00:00:00+00:00', 4)"
    )
    legacy.executemany(
        "INSERT INTO readings (batch_id, seq, local_time, utc_time, reading, row_status) "
        "VALUES (1, ?, ?, ?, ?, ?)",
        [
            (1, "2026-11-01T01:00:00", "2026-11-01T05:00:00+00:00", 10.0, "ambiguous"),
            (2, "2026-11-01T01:59:00", "2026-11-01T05:59:00+00:00", 11.0, "ambiguous"),
            (3, "2026-11-01T01:00:00", "2026-11-01T06:00:00+00:00", 12.0, "ambiguous"),
            (4, "2026-11-01T02:00:00", "2026-11-01T07:00:00+00:00", 13.0, "normal"),
        ],
    )
    legacy.commit()
    legacy.close()

    conn = connect()
    init_db(conn)
    try:
        batch = get_published_batch(conn, "LEG-1", date(2026, 11, 1))
    finally:
        conn.close()

    folds = {r["seq"]: r["fold"] for r in batch["readings"]}
    assert folds == {1: 0, 2: 0, 3: 1, 4: 0}

    # 迁移后跨日比对可正常工作：次日同钟点同读数上传应被识别为重传
    from app.main import app

    with TestClient(app) as c:
        dup = (
            "seq,local_time,reading\n"
            "1,01:00,10\n2,01:59,11\n3,02:00,13\n"
            "4,03:00,14\n5,03:01,15\n"
        )
        # 仅 3 条能与旧批次匹配（fold=0 段），不足 5 连 → 发布
        r = c.post(
            "/meters/LEG-1/dates/2026-11-02/batches",
            files={"file": ("r.csv", io.BytesIO(dup.encode()), "text/csv")},
        )
        assert r.status_code == 200, r.text


def test_solve_batch_assigns_fold_field():
    # 回拨日完整时序：第二次经过 01 点的行 fold 必须为 1
    from app.tzmap import RowInput

    inputs = [
        RowInput(seq=1, local_time=datetime(2026, 11, 1, 1, 0), reading=10.0),
        RowInput(seq=2, local_time=datetime(2026, 11, 1, 1, 59), reading=11.0),
        RowInput(seq=3, local_time=datetime(2026, 11, 1, 1, 0), reading=12.0),
        RowInput(seq=4, local_time=datetime(2026, 11, 1, 1, 59), reading=13.0),
    ]
    res = solve_batch(inputs)
    assert [a.fold for a in res.assigned] == [0, 0, 1, 1]
    assert all(a.status is RowStatus.AMBIGUOUS for a in res.assigned)


def test_immediate_transaction_commits_and_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("HEATMETER_DB", str(tmp_path / "tx.db"))
    conn = connect()
    init_db(conn)

    # 正常提交
    with immediate_transaction(conn):
        conn.execute(
            "INSERT INTO batches (meter_id, local_date, published_at, row_count) "
            "VALUES ('TX-1', '2026-01-01', '2026-01-01T00:00:00+00:00', 0)"
        )
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1

    # 异常回滚
    with pytest.raises(RuntimeError):
        with immediate_transaction(conn):
            conn.execute(
                "INSERT INTO batches (meter_id, local_date, published_at, row_count) "
                "VALUES ('TX-2', '2026-01-02', '2026-01-02T00:00:00+00:00', 0)"
            )
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
    conn.close()
