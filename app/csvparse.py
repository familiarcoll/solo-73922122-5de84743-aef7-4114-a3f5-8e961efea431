"""上传 CSV 的严格解析与校验。

CSV 要求（表头固定三列，顺序固定）：
    seq,local_time,reading
* 数据行 1–500 行；
* seq：正整数，全表严格递增；
* local_time：分钟级纽约本地时间，支持 ``HH:MM`` / ``HH:MM:SS``（秒须为 0）
  或完整 ISO 时间（日期必须等于上传参数 local_date）；
* reading：有限浮点数（拒绝 NaN/Infinity）。
"""

from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime, time
from typing import Any

from .tzmap import RowInput

HEADER = ("seq", "local_time", "reading")
MAX_ROWS = 500

# 可接受的纯时间格式
_TIME_FORMATS = ("%H:%M", "%H:%M:%S")
# 可接受的完整日期时间格式
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


class CsvValidationError(Exception):
    """CSV 结构或字段校验失败，message 为面向用户的中文说明。"""

    def __init__(self, message: str, row: int | None = None, seq: Any = None):
        super().__init__(message)
        self.message = message
        self.row = row  # CSV 物理行号（含表头，从 1 开始）
        self.seq = seq


def _parse_seq(raw: str, row_no: int) -> int:
    raw = raw.strip()
    if not raw:
        raise CsvValidationError("seq 不能为空", row=row_no)
    try:
        value = int(raw)
    except ValueError:
        raise CsvValidationError(
            f"seq 必须为整数，收到 {raw!r}", row=row_no, seq=raw
        ) from None
    if value < 1:
        raise CsvValidationError(
            f"seq 必须为正整数（>=1），收到 {value}", row=row_no, seq=value
        )
    return value


def _parse_local_time(raw: str, local_date: date, row_no: int) -> datetime:
    raw = raw.strip()
    if not raw:
        raise CsvValidationError("local_time 不能为空", row=row_no)

    # 纯钟点：与上传参数 local_date 组合
    for fmt in _TIME_FORMATS:
        try:
            t = datetime.strptime(raw, fmt).time()
            break
        except ValueError:
            t = None
    if t is not None:
        if t.second != 0:
            raise CsvValidationError(
                f"local_time 必须为分钟精度（秒为 0），收到 {raw!r}", row=row_no
            )
        return datetime.combine(local_date, t.replace(second=0, microsecond=0))

    # 完整日期时间：日期必须与 local_date 一致（整日数据按日期归属）
    parsed: datetime | None = None
    for fmt in _DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise CsvValidationError(
            f"local_time 格式无法识别，收到 {raw!r}；"
            "支持 HH:MM、HH:MM:SS 或 YYYY-MM-DDTHH:MM:SS",
            row=row_no,
        )
    if parsed.date() != local_date:
        raise CsvValidationError(
            f"local_time 日期 {parsed.date().isoformat()} 与上传日期 "
            f"{local_date.isoformat()} 不一致；整日数据只接受该日期的钟点",
            row=row_no,
        )
    if parsed.second != 0 or parsed.microsecond != 0:
        raise CsvValidationError(
            f"local_time 必须为分钟精度，收到 {raw!r}", row=row_no
        )
    return parsed.replace(second=0, microsecond=0)


def _parse_reading(raw: str, row_no: int) -> float:
    raw = raw.strip()
    if not raw:
        raise CsvValidationError("reading 不能为空", row=row_no)
    try:
        value = float(raw)
    except ValueError:
        raise CsvValidationError(
            f"reading 必须为数字，收到 {raw!r}", row=row_no
        ) from None
    if not math.isfinite(value):
        raise CsvValidationError(
            f"reading 必须为有限数值，拒绝 NaN/Infinity，收到 {raw!r}", row=row_no
        )
    return value


def parse_readings_csv(content: bytes | str, local_date: date) -> list[RowInput]:
    """解析并校验上传 CSV，返回 RowInput 列表；失败抛 CsvValidationError。"""
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CsvValidationError("CSV 必须为 UTF-8 编码文本") from None
    else:
        text = content

    if not text.strip():
        raise CsvValidationError("CSV 内容为空")

    reader = csv.reader(io.StringIO(text))
    try:
        all_rows = list(reader)
    except csv.Error as exc:
        raise CsvValidationError(f"CSV 解析失败：{exc}") from None

    if not all_rows:
        raise CsvValidationError("CSV 缺少表头")

    header = tuple(h.strip() for h in all_rows[0])
    if header != HEADER:
        raise CsvValidationError(
            f"CSV 表头必须严格为 {','.join(HEADER)}，收到 {','.join(header) or '(空)'}"
        )

    data_rows = all_rows[1:]
    # 过滤纯空行（尾部换行产生的空记录），其余列数错误一律拒绝
    cleaned: list[list[str]] = []
    for idx, row in enumerate(data_rows, start=2):
        if len(row) == 1 and row[0].strip() == "":
            continue
        if len(row) != 3:
            raise CsvValidationError(
                f"每行必须恰好包含 3 列（seq,local_time,reading），该行有 {len(row)} 列",
                row=idx,
            )
        cleaned.append(row)

    if not cleaned:
        raise CsvValidationError("CSV 没有任何数据行")
    if len(cleaned) > MAX_ROWS:
        raise CsvValidationError(
            f"数据行数 {len(cleaned)} 超过上限 {MAX_ROWS} 行"
        )

    result: list[RowInput] = []
    prev_seq: int | None = None
    for offset, row in enumerate(cleaned):
        row_no = offset + 2  # 物理行号（表头为第 1 行）
        seq = _parse_seq(row[0], row_no)
        if prev_seq is not None and seq <= prev_seq:
            raise CsvValidationError(
                f"seq 必须严格递增：{prev_seq} 之后收到 {seq}",
                row=row_no,
                seq=seq,
            )
        local_time = _parse_local_time(row[1], local_date, row_no)
        reading = _parse_reading(row[2], row_no)
        result.append(RowInput(seq=seq, local_time=local_time, reading=reading))
        prev_seq = seq

    return result
