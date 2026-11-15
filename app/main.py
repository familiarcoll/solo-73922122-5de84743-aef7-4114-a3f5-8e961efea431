"""FastAPI 应用：楼宇热量表纽约本地读数 -> UTC 入仓。

端点：
* POST /meters/{meter_id}/dates/{local_date}/batches  上传 CSV（multipart 表单）
* GET  /meters/{meter_id}/dates/{local_date}/readings 查询该日已发布 UTC 读数
* GET  /meters/{meter_id}/dates                        列出已发布日期
* GET  /healthz                                       健康检查
* GET  /docs                                          Swagger UI（自动生成）
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI, File, HTTPException, Path, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .csvparse import CsvValidationError, parse_readings_csv
from .db import (
    connect,
    get_published_batch,
    immediate_transaction,
    init_db,
    insert_batch_locked,
    list_published_dates,
)
from .dedup import (
    MIN_DISTINCT_READINGS,
    MIN_MATCH_RUN,
    MatchedRun,
    current_day_readings,
    detect_reupload,
    previous_day_readings,
)
from .tzmap import BatchOutcome, RowStatus, solve_batch

# 表号：字母数字与连字符，2–64 位，防止路径/注入类异常输入
_METER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$")

_OUTCOME_HTTP = {
    BatchOutcome.GAP_REJECTED: status.HTTP_422_UNPROCESSABLE_ENTITY,
    BatchOutcome.NO_SOLUTION: status.HTTP_422_UNPROCESSABLE_ENTITY,
    BatchOutcome.MULTIPLE_SOLUTIONS: status.HTTP_422_UNPROCESSABLE_ENTITY,
}

_OUTCOME_CODE = {
    BatchOutcome.GAP_REJECTED: "SPRING_GAP_NONEXISTENT_LOCAL_TIME",
    BatchOutcome.NO_SOLUTION: "NO_UNIQUE_MAPPING_NO_SOLUTION",
    BatchOutcome.MULTIPLE_SOLUTIONS: "NO_UNIQUE_MAPPING_MULTIPLE_SOLUTIONS",
}

# 跨日重传（前一日读数换日期再传）拒绝码
_DUPLICATE_CODE = "DUPLICATE_OF_PREVIOUS_DAY_BATCH"


class ReadingOut(BaseModel):
    seq: int
    local_time: str = Field(description="纽约本地挂钟时间（naive ISO）")
    utc_time: str = Field(description="映射后的 UTC 时间（带偏移 ISO 8601）")
    reading: float
    mapping: str = Field(description="normal=普通钟点；ambiguous=秋季回拨（已按整批唯一解定档）")
    fold: int = Field(
        default=0,
        description="回拨档位：0=第一次经过（fold=0/普通钟点），1=回拨后第二次经过（fold=1）",
    )


class BatchResponse(BaseModel):
    status: str
    meter_id: str
    local_date: str
    batch_id: int
    row_count: int
    published_at: str
    replaced_batch_id: int | None = None
    previous_published_at: str | None = None
    ambiguous_seq: list[int] = Field(
        default_factory=list, description="本批中落在秋季重复钟点、已被唯一确定的序号"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="楼宇热量表 UTC 入仓服务",
    description=(
        "纽约本地时钟（America/New_York）分钟级读数 CSV 上传，"
        "按整批 UTC 严格递增约束求解唯一映射后入 SQLite。"
        "春季缺口、无解、多解均拒绝且保留该日此前发布数据；"
        "唯一映射后再与同一表前一纽约本地日的已发布批次做跨日重传比对，"
        "连续 5 条同钟点、同回拨档位且读数相等（含至少 3 个不同读数）即拒绝整批。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def _validate_meter(meter_id: str) -> None:
    if not _METER_RE.match(meter_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "meter_id 格式非法：需以字母或数字开头，仅含字母、数字、下划线、连字符，"
                f"长度 2–64，收到 {meter_id!r}"
            ),
        )


def _rejection_payload(
    meter_id: str,
    local_date: date,
    code: str,
    reason: str,
    involved_seqs: list[int],
    retained: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "error_code": code,
        "meter_id": meter_id,
        "local_date": local_date.isoformat(),
        "reason": reason,
        "involved_seqs": involved_seqs,
        "retained_previous_batch": retained is not None,
    }
    if retained is not None:
        detail["retained_batch"] = {
            "batch_id": retained["batch_id"],
            "published_at": retained["published_at"],
            "row_count": retained["row_count"],
        }
    if extra:
        detail.update(extra)
    return {"detail": detail}


class DuplicateRejected(Exception):
    """锁内跨日比对命中重传：携带已构造好的拒绝载荷，事务须回滚。"""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload["detail"]["error_code"])
        self.payload = payload


@app.post(
    "/meters/{meter_id}/dates/{local_date}/batches",
    response_model=BatchResponse,
    status_code=status.HTTP_200_OK,
    summary="上传一日读数 CSV：UTC 唯一映射后做跨日重传比对，通过则整批发布（重传完整替换）",
)
async def upload_batch(
    meter_id: str = Path(..., description="热量表表号"),
    local_date: date = Path(..., description="本地日期 YYYY-MM-DD"),
    file: UploadFile = File(..., description="CSV：seq,local_time,reading 三列，至多 500 行"),
):
    _validate_meter(meter_id)

    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件为空"
        )

    # 1. CSV 结构/字段校验（失败不触碰数据库）
    try:
        rows = parse_readings_csv(content, local_date)
    except CsvValidationError as exc:
        detail: dict[str, Any] = {"error_code": "CSV_VALIDATION_FAILED", "reason": exc.message}
        if exc.row is not None:
            detail["csv_row"] = exc.row
        if exc.seq is not None:
            detail["seq"] = exc.seq
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=detail
        ) from None

    # 2. 整批 UTC 映射求解
    result = solve_batch(rows)

    if result.outcome is not BatchOutcome.UNIQUE:
        # 3. 拒绝：查一下此前是否有发布批次以便说明保留情况（只读，不写入）
        conn = connect()
        try:
            retained = get_published_batch(conn, meter_id, local_date)
        finally:
            conn.close()
        return JSONResponse(
            status_code=_OUTCOME_HTTP[result.outcome],
            content=_rejection_payload(
                meter_id,
                local_date,
                _OUTCOME_CODE[result.outcome],
                result.reason or "映射不唯一",
                result.involved_seqs,
                retained,
            ),
        )

    # 4. 唯一解：在同一个 BEGIN IMMEDIATE 事务内完成
    #    “取前日已发布批次 → 跨日重传比对 → 当日整批替换发布”。
    #    持锁期间其他连接无法替换前日批次，比对与发布必然基于同一已提交版本。
    previous_date = local_date - timedelta(days=1)
    conn = connect()
    try:
        with immediate_transaction(conn):
            previous = get_published_batch(conn, meter_id, previous_date)
            if previous is not None:
                run: MatchedRun | None = detect_reupload(
                    current_day_readings(result.assigned),
                    previous_day_readings(previous["readings"]),
                )
                if run is not None:
                    # 命中跨日重传：不写入任何数据，抛出后事务回滚，
                    # 当日原已发布批次原样保留。
                    retained_today = get_published_batch(
                        conn, meter_id, local_date
                    )
                    payload = _rejection_payload(
                        meter_id,
                        local_date,
                        _DUPLICATE_CODE,
                        (
                            f"与同一表前一纽约本地日 {previous_date.isoformat()} "
                            f"的已发布批次（batch_id={previous['batch_id']}）存在 "
                            f"{run.length} 条连续读数在相同本地钟点与回拨档位上"
                            f"逐条相等，且含 {run.distinct_readings} 个不同读数"
                            f"（阈值：连续≥{MIN_MATCH_RUN} 条、不同读数≥"
                            f"{MIN_DISTINCT_READINGS} 个），判定为前日读数换日期"
                            "重传，按规则拒绝整批"
                        ),
                        run.current_seqs,
                        retained_today,
                        extra={
                            "previous_local_date": previous_date.isoformat(),
                            "previous_batch_id": previous["batch_id"],
                            "matched_run_length": run.length,
                            "matched_distinct_readings": run.distinct_readings,
                            "previous_involved_seqs": run.previous_seqs,
                        },
                    )
                    raise DuplicateRejected(payload)

            # 无前日批次，或比对未命中：单事务完整替换发布当日批次
            info = insert_batch_locked(
                conn, meter_id, local_date, result.assigned
            )
    except DuplicateRejected as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=exc.payload,
        )
    finally:
        conn.close()

    ambiguous_seq = [
        r.seq for r in result.assigned if r.status is RowStatus.AMBIGUOUS
    ]
    return BatchResponse(
        status="published",
        meter_id=meter_id,
        local_date=local_date.isoformat(),
        batch_id=info["batch_id"],
        row_count=info["row_count"],
        published_at=info["published_at"],
        replaced_batch_id=info["replaced_batch_id"],
        previous_published_at=info["previous_published_at"],
        ambiguous_seq=ambiguous_seq,
    )


@app.get(
    "/meters/{meter_id}/dates/{local_date}/readings",
    summary="查询该日已发布批次的 UTC 读数（仅返回同一完整批次）",
)
async def read_readings(
    meter_id: str = Path(..., description="热量表表号"),
    local_date: date = Path(..., description="本地日期 YYYY-MM-DD"),
):
    _validate_meter(meter_id)
    conn = connect()
    try:
        batch = get_published_batch(conn, meter_id, local_date)
    finally:
        conn.close()

    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "NO_PUBLISHED_BATCH",
                "reason": (
                    f"表 {meter_id} 在 {local_date.isoformat()} 尚无已发布批次；"
                    "被拒绝的上传不会产生数据"
                ),
            },
        )

    return {
        "meter_id": batch["meter_id"],
        "local_date": batch["local_date"],
        "batch_id": batch["batch_id"],
        "published_at": batch["published_at"],
        "row_count": batch["row_count"],
        "readings": [
            {
                "seq": r["seq"],
                "local_time": r["local_time"],
                "utc_time": r["utc_time"],
                "reading": r["reading"],
                "mapping": r["row_status"],
                "fold": r["fold"],
            }
            for r in batch["readings"]
        ],
    }


@app.get("/meters/{meter_id}/dates", summary="列出该表所有已发布日期与批次")
async def read_dates(meter_id: str):
    _validate_meter(meter_id)
    conn = connect()
    try:
        items = list_published_dates(conn, meter_id)
    finally:
        conn.close()
    return {"meter_id": meter_id, "batches": items}


@app.get("/healthz", summary="健康检查")
async def healthz():
    return {"status": "ok"}
