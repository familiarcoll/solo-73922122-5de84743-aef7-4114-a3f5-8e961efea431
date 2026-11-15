"""端到端与单元测试：上传/查询/保留/替换/CSV 校验/春秋令时。"""

from __future__ import annotations

import io
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app.csvparse import CsvValidationError, parse_readings_csv
from app.db import connect, get_published_batch, init_db
from app.tzmap import BatchOutcome, RowInput, RowStatus, local_candidates, solve_batch


# ---------------------------------------------------------------- 单元：候选映射


def test_local_candidates_normal():
    status, cand = local_candidates(datetime(2026, 6, 15, 12, 0))
    assert status is RowStatus.NORMAL
    assert len(cand) == 1
    assert cand[0].isoformat() == "2026-06-15T16:00:00+00:00"  # EDT UTC-4


def test_local_candidates_spring_gap():
    # 2026-03-08 02:30 在纽约不存在
    status, cand = local_candidates(datetime(2026, 3, 8, 2, 30))
    assert status is RowStatus.GAP
    assert cand == ()
    # 缺口边界之外仍是正常钟点
    assert local_candidates(datetime(2026, 3, 8, 1, 59))[0] is RowStatus.NORMAL
    assert local_candidates(datetime(2026, 3, 8, 3, 0))[0] is RowStatus.NORMAL


def test_local_candidates_fall_fold_two_candidates():
    # 2026-11-01 01:30 重复：EDT(UTC-4)=05:30Z，EST(UTC-5)=06:30Z
    status, cand = local_candidates(datetime(2026, 11, 1, 1, 30))
    assert status is RowStatus.AMBIGUOUS
    assert [c.isoformat() for c in cand] == [
        "2026-11-01T05:30:00+00:00",
        "2026-11-01T06:30:00+00:00",
    ]


# ---------------------------------------------------------------- 单元：整批求解


def _ri(seq: int, y: int, mo: int, d: int, h: int, mi: int, reading: float = 1.0) -> RowInput:
    return RowInput(seq=seq, local_time=datetime(y, mo, d, h, mi), reading=reading)


def test_solve_unique_fold_single_ambiguous_between_anchors():
    # 单个重复钟点被前后正常锚点夹住：EDT 档 05:30Z 与 EST 档 06:30Z 均可行？
    # 前置 00:59=04:59Z、后置 02:30=07:30Z —— 两档都可行，故改为前置锚点收紧：
    # 前置取 01:59 EDT(05:59Z)，则 01:30 的 EDT 档(05:30Z) 不可行，只能 EST(06:30Z)。
    rows = [
        _ri(1, 2026, 11, 1, 1, 59),  # ambiguous，唯一可行档 EDT=05:59Z
        _ri(2, 2026, 11, 1, 1, 30),  # ambiguous，被强制 EST=06:30Z
        _ri(3, 2026, 11, 1, 2, 30),  # normal EST -> 07:30Z
    ]
    res = solve_batch(rows)
    assert res.outcome is BatchOutcome.UNIQUE
    assert res.assigned[0].utc_time.isoformat() == "2026-11-01T05:59:00+00:00"
    assert res.assigned[1].utc_time.isoformat() == "2026-11-01T06:30:00+00:00"
    assert res.assigned[2].utc_time.isoformat() == "2026-11-01T07:30:00+00:00"
    assert res.assigned[0].status is RowStatus.AMBIGUOUS
    assert res.assigned[1].status is RowStatus.AMBIGUOUS
    assert res.assigned[2].status is RowStatus.NORMAL


def test_solve_unique_fold_full_chronological_block():
    # 时钟真实走法：01:00→01:59（EDT），回拨后再 01:00→01:59（EST）。
    # 第二块的每个重复钟点都被前一行严格递增约束钉死在 EST 档，整批唯一。
    rows = [
        _ri(1, 2026, 11, 1, 1, 0),    # EDT 05:00Z
        _ri(2, 2026, 11, 1, 1, 59),   # EDT 05:59Z
        _ri(3, 2026, 11, 1, 1, 0),    # 重复：EDT 档 05:00Z 不可行 -> EST 06:00Z
        _ri(4, 2026, 11, 1, 1, 59),   # EST 06:59Z
    ]
    res = solve_batch(rows)
    assert res.outcome is BatchOutcome.UNIQUE
    assert [r.utc_time.isoformat() for r in res.assigned] == [
        "2026-11-01T05:00:00+00:00",
        "2026-11-01T05:59:00+00:00",
        "2026-11-01T06:00:00+00:00",
        "2026-11-01T06:59:00+00:00",
    ]
    assert all(r.status is RowStatus.AMBIGUOUS for r in res.assigned)


def test_solve_fold_two_ambiguous_rows_multiple_solutions():
    # 两个连续重复钟点、无锚点：EDT/EST 两档都合法 -> 多解
    rows = [
        _ri(1, 2026, 11, 1, 1, 0),
        _ri(2, 2026, 11, 1, 1, 30),
    ]
    res = solve_batch(rows)
    assert res.outcome is BatchOutcome.MULTIPLE_SOLUTIONS
    assert res.involved_seqs == [1, 2]
    assert res.assigned == []


def test_solve_fold_second_occurrence_forced():
    # 前置锚点 01:59 EDT(05:59Z) 之后再来 01:00，只能取 EST(06:00Z)
    rows = [
        _ri(1, 2026, 11, 1, 1, 59),
        _ri(2, 2026, 11, 1, 1, 0),   # ambiguous，EDT 档 05:00Z < 05:59Z 不可行
        _ri(3, 2026, 11, 1, 1, 30),  # ambiguous，必须 EST 06:30Z
    ]
    res = solve_batch(rows)
    assert res.outcome is BatchOutcome.UNIQUE
    assert res.assigned[1].utc_time.isoformat() == "2026-11-01T06:00:00+00:00"
    assert res.assigned[2].utc_time.isoformat() == "2026-11-01T06:30:00+00:00"


def test_solve_no_solution_on_backward_local_time():
    rows = [
        _ri(1, 2026, 6, 15, 10, 0),
        _ri(2, 2026, 6, 15, 9, 0),  # 普通钟点倒流，UTC 必然倒流
    ]
    res = solve_batch(rows)
    assert res.outcome is BatchOutcome.NO_SOLUTION
    assert 2 in res.involved_seqs


def test_solve_duplicate_normal_local_time_no_solution():
    rows = [
        _ri(1, 2026, 6, 15, 10, 0),
        _ri(2, 2026, 6, 15, 10, 0),  # 同一普通钟点两次，无法严格递增
    ]
    res = solve_batch(rows)
    assert res.outcome is BatchOutcome.NO_SOLUTION


def test_solve_spring_gap_rejected_with_seqs():
    rows = [
        _ri(1, 2026, 3, 8, 1, 59),
        _ri(2, 2026, 3, 8, 2, 0),
        _ri(3, 2026, 3, 8, 2, 59),
        _ri(4, 2026, 3, 8, 3, 0),
    ]
    res = solve_batch(rows)
    assert res.outcome is BatchOutcome.GAP_REJECTED
    assert res.involved_seqs == [2, 3]


# ---------------------------------------------------------------- CSV 解析


def test_csv_basic_and_time_formats(tmp_path):
    csv_text = (
        "seq,local_time,reading\n"
        "1,00:00,12.5\n"
        "2,00:01:00,13\n"
        "3,2026-06-15T00:02:00,-4.25\n"
    )
    rows = parse_readings_csv(csv_text, date(2026, 6, 15))
    assert [r.seq for r in rows] == [1, 2, 3]
    assert rows[2].reading == -4.25
    assert rows[0].local_time.second == 0


def test_csv_rejects_bad_header():
    with pytest.raises(CsvValidationError) as ei:
        parse_readings_csv("a,b,c\n1,00:00,1\n", date(2026, 6, 15))
    assert "表头" in ei.value.message


def test_csv_rejects_non_increasing_seq():
    csv_text = "seq,local_time,reading\n1,00:00,1\n1,00:01,2\n"
    with pytest.raises(CsvValidationError) as ei:
        parse_readings_csv(csv_text, date(2026, 6, 15))
    assert "严格递增" in ei.value.message
    assert ei.value.row == 3


def test_csv_rejects_nan_reading():
    csv_text = "seq,local_time,reading\n1,00:00,NaN\n"
    with pytest.raises(CsvValidationError) as ei:
        parse_readings_csv(csv_text, date(2026, 6, 15))
    assert "有限" in ei.value.message


def test_csv_rejects_seconds_precision():
    csv_text = "seq,local_time,reading\n1,00:00:30,1\n"
    with pytest.raises(CsvValidationError):
        parse_readings_csv(csv_text, date(2026, 6, 15))


def test_csv_rejects_wrong_date():
    csv_text = "seq,local_time,reading\n1,2026-06-16T00:00:00,1\n"
    with pytest.raises(CsvValidationError) as ei:
        parse_readings_csv(csv_text, date(2026, 6, 15))
    assert "不一致" in ei.value.message


def test_csv_rejects_over_500_rows():
    lines = ["seq,local_time,reading"]
    lines += [f"{i},{i // 60:02d}:{i % 60:02d},1" for i in range(1, 502)]
    with pytest.raises(CsvValidationError) as ei:
        parse_readings_csv("\n".join(lines) + "\n", date(2026, 6, 15))
    assert "500" in ei.value.message


# ---------------------------------------------------------------- API 端到端


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


def test_health_and_docs(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    docs = client.get("/docs")
    assert docs.status_code == 200
    openapi = client.get("/openapi.json").json()
    assert "/meters/{meter_id}/dates/{local_date}/batches" in openapi["paths"]


def test_upload_normal_day_publishes_utc(client):
    d = date(2026, 6, 15)
    csv_text = (
        "seq,local_time,reading\n"
        "1,00:00,100.0\n"
        "2,00:01,101.5\n"
        "3,23:59,200.25\n"
    )
    r = _upload(client, "M-001", d, csv_text)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "published"
    assert body["row_count"] == 3
    assert body["replaced_batch_id"] is None

    q = client.get(f"/meters/M-001/dates/{d.isoformat()}/readings")
    assert q.status_code == 200
    readings = q.json()["readings"]
    assert [x["seq"] for x in readings] == [1, 2, 3]
    assert readings[0]["utc_time"] == "2026-06-15T04:00:00+00:00"
    assert readings[1]["utc_time"] == "2026-06-15T04:01:00+00:00"
    assert readings[2]["utc_time"] == "2026-06-15T23:59:00-04:00" or readings[2][
        "utc_time"
    ] == "2026-06-16T03:59:00+00:00"
    assert all(x["mapping"] == "normal" for x in readings)


def test_spring_gap_rejected_and_retains_previous(client):
    d = date(2026, 3, 8)
    good = "seq,local_time,reading\n1,01:59,10\n2,03:00,11\n"
    r1 = _upload(client, "M-002", d, good)
    assert r1.status_code == 200
    first_batch = r1.json()["batch_id"]

    bad = (
        "seq,local_time,reading\n"
        "1,01:59,10\n"
        "2,02:00,11\n"   # gap
        "3,02:59,12\n"   # gap
        "4,03:00,13\n"
    )
    r2 = _upload(client, "M-002", d, bad)
    assert r2.status_code == 422
    detail = r2.json()["detail"]
    assert detail["error_code"] == "SPRING_GAP_NONEXISTENT_LOCAL_TIME"
    assert detail["involved_seqs"] == [2, 3]
    assert detail["retained_previous_batch"] is True
    assert detail["retained_batch"]["batch_id"] == first_batch

    # 查询仍返回旧批次
    q = client.get(f"/meters/M-002/dates/{d.isoformat()}/readings").json()
    assert q["batch_id"] == first_batch
    assert q["row_count"] == 2


def test_fall_fold_unique_mapping_publishes(client):
    d = date(2026, 11, 1)
    # 时钟真实走法：01:00/01:59（EDT），回拨后重复 01:00/01:59（EST），再到 02:30
    csv_text = (
        "seq,local_time,reading\n"
        "1,01:00,10\n"   # ambiguous -> EDT 05:00Z
        "2,01:59,11\n"   # ambiguous -> EDT 05:59Z
        "3,01:00,12\n"   # 重复，EDT 档不可行 -> EST 06:00Z
        "4,01:59,13\n"   # ambiguous -> EST 06:59Z
        "5,02:30,14\n"   # normal    -> EST 07:30Z
    )
    r = _upload(client, "M-003", d, csv_text)
    assert r.status_code == 200, r.text
    assert r.json()["ambiguous_seq"] == [1, 2, 3, 4]

    q = client.get(f"/meters/M-003/dates/{d.isoformat()}/readings").json()
    utcs = [x["utc_time"] for x in q["readings"]]
    assert utcs == [
        "2026-11-01T05:00:00+00:00",
        "2026-11-01T05:59:00+00:00",
        "2026-11-01T06:00:00+00:00",
        "2026-11-01T06:59:00+00:00",
        "2026-11-01T07:30:00+00:00",
    ]
    # UTC 严格递增
    assert utcs == sorted(utcs) and len(set(utcs)) == 5


def test_fall_fold_multiple_solutions_rejected_with_seqs(client):
    d = date(2026, 11, 1)
    csv_text = (
        "seq,local_time,reading\n"
        "1,01:00,10\n"
        "2,01:30,11\n"
    )
    r = _upload(client, "M-004", d, csv_text)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error_code"] == "NO_UNIQUE_MAPPING_MULTIPLE_SOLUTIONS"
    assert detail["involved_seqs"] == [1, 2]
    assert detail["retained_previous_batch"] is False

    assert (
        client.get(f"/meters/M-004/dates/{d.isoformat()}/readings").status_code
        == 404
    )


def test_no_solution_rejected_and_retains(client):
    d = date(2026, 6, 15)
    r1 = _upload(
        client,
        "M-005",
        d,
        "seq,local_time,reading\n1,10:00,1\n2,10:01,2\n",
    )
    assert r1.status_code == 200
    old_id = r1.json()["batch_id"]

    r2 = _upload(
        client,
        "M-005",
        d,
        "seq,local_time,reading\n1,10:00,1\n2,09:59,2\n",  # 倒流
    )
    assert r2.status_code == 422
    detail = r2.json()["detail"]
    assert detail["error_code"] == "NO_UNIQUE_MAPPING_NO_SOLUTION"
    assert detail["involved_seqs"] == [2]
    assert detail["retained_previous_batch"] is True

    q = client.get(f"/meters/M-005/dates/{d.isoformat()}/readings").json()
    assert q["batch_id"] == old_id


def test_successful_reupload_replaces_whole_day(client):
    d = date(2026, 6, 15)
    r1 = _upload(
        client, "M-006", d, "seq,local_time,reading\n1,00:00,1\n2,00:01,2\n"
    )
    old_id = r1.json()["batch_id"]

    r2 = _upload(
        client,
        "M-006",
        d,
        "seq,local_time,reading\n1,00:00,9\n2,00:01,8\n3,00:02,7\n",
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["replaced_batch_id"] == old_id
    assert body["row_count"] == 3

    q = client.get(f"/meters/M-006/dates/{d.isoformat()}/readings").json()
    assert q["batch_id"] != old_id
    assert [x["reading"] for x in q["readings"]] == [9.0, 8.0, 7.0]

    # 旧批次读数已物理清除，库里只剩当前批次
    import os

    conn = connect(os.environ["HEATMETER_DB"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 3
    finally:
        conn.close()


def test_csv_validation_error_does_not_touch_db(client):
    d = date(2026, 6, 15)
    r = _upload(client, "M-007", d, "seq,local_time,reading\n1,00:00,NaN\n")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "CSV_VALIDATION_FAILED"
    assert client.get(f"/meters/M-007/dates/{d.isoformat()}/readings").status_code == 404


def test_meter_id_validation(client):
    # 含点号等非法字符，路径可匹配但被表号规则拒绝（最小长度 2）
    r = _upload(
        client, "evil.meter", date(2026, 6, 15),
        "seq,local_time,reading\n1,00:00,1\n",
    )
    assert r.status_code == 400
    assert "meter_id" in r.json()["detail"]


def test_dates_listing_and_404(client):
    assert client.get("/meters/M-008/dates").json()["batches"] == []
    assert (
        client.get(f"/meters/M-008/dates/{date(2026, 6, 15).isoformat()}/readings").status_code
        == 404
    )
    _upload(
        client, "M-008", date(2026, 6, 15),
        "seq,local_time,reading\n1,00:00,1\n",
    )
    listing = client.get("/meters/M-008/dates").json()["batches"]
    assert len(listing) == 1
    assert listing[0]["local_date"] == "2026-06-15"


def test_full_fold_day_240_minutes_unique(client):
    """秋季整日 25 小时分钟级数据：按真实发生顺序排列，01:00–01:59 各两次。

    顺序：00:00–00:59（EDT）→ 01:00–01:59 第一次（EDT）
         → 01:00–01:59 第二次（EST）→ 02:00–02:59（EST）。共 240 行，映射唯一。
    """
    d = date(2026, 11, 1)
    lines = ["seq,local_time,reading"]
    seq = 1

    def add_block(h: int):
        nonlocal seq
        for mi in range(60):
            lines.append(f"{seq},{h:02d}:{mi:02d},{seq}")
            seq += 1

    add_block(0)
    add_block(1)  # 第一次经过 01 点（EDT）
    add_block(1)  # 回拨后第二次经过 01 点（EST）
    add_block(2)
    csv_text = "\n".join(lines) + "\n"
    assert len(lines) - 1 == 240

    r = _upload(client, "M-009", d, csv_text)
    assert r.status_code == 200, r.text
    q = client.get(f"/meters/M-009/dates/{d.isoformat()}/readings").json()
    utcs = [x["utc_time"] for x in q["readings"]]
    assert len(utcs) == 240
    assert utcs == sorted(utcs) and len(set(utcs)) == 240
    # 首末 UTC：00:00 EDT=04:00Z；02:59 EST=07:59Z
    assert utcs[0] == "2026-11-01T04:00:00+00:00"
    assert utcs[-1] == "2026-11-01T07:59:00+00:00"
    # 两个 01 点块共 120 行落在重复钟点
    assert len([x for x in q["readings"] if x["mapping"] == "ambiguous"]) == 120
    # 第二次 01:00 必须落在 EST 档 06:00Z
    second_one_oclock = next(
        x for x in q["readings"]
        if x["local_time"] == "2026-11-01T01:00:00" and x["seq"] == 121
    )
    assert second_one_oclock["utc_time"] == "2026-11-01T06:00:00+00:00"
