"""跨日重复批次检测（网关把前一天读数换日期重传的识别）。

当日批次在通过原有的整批 UTC 唯一映射之后，与同一表 **前一纽约本地日**
（按本地日历 local_date - 1，America/New_York 日历）的已发布批次比对：

* 两条读数在 **相同本地挂钟钟点**（HH:MM，与日期无关）且
  **相同回拨档位 fold**（0=第一次经过/fold=0，1=回拨后第二次经过/fold=1）
  且 **reading 逐条相等** 时构成一个“匹配点”；
* 两天各自按时间顺序排列的匹配点出现 **连续至少 ``MIN_MATCH_RUN`` 条**
  （最长公共子串语义，序列两侧同时连续），并且该连续段内包含至少
  ``MIN_DISTINCT_READINGS`` 个不同读数（避免表停走/恒定值误判），
  即判定为换日期重传，整批拒绝。

比对只依赖各批次已确定的 (钟点, fold, reading) 三元组，与具体日期、
UTC 偏移无关；fold 直接随发布结果持久化，历史数据由 db 迁移回填。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# 触发拒绝所需的最短连续匹配段
MIN_MATCH_RUN = 5
# 匹配段内至少需要的不同读数个数
MIN_DISTINCT_READINGS = 3


@dataclass(frozen=True)
class ReadingKey:
    """跨日比对用的读数身份：本地钟点（HH:MM）+ 回拨档位 + 读数。"""

    clock: str
    fold: int
    reading: float


@dataclass(frozen=True)
class DayReading:
    """参与比对的一天中的一条读数（按 UTC 升序后的时间顺序）。"""

    seq: int
    clock: str
    fold: int
    reading: float


@dataclass(frozen=True)
class MatchedRun:
    """一段满足阈值的连续匹配。"""

    length: int
    distinct_readings: int
    current_seqs: list[int]   # 当日批次涉及序号（升序）
    previous_seqs: list[int]  # 前日批次对应序号（升序）


def reading_key(clock: str, fold: int, reading: float) -> ReadingKey:
    return ReadingKey(clock=clock, fold=fold, reading=reading)


def current_day_readings(assigned: list) -> list[DayReading]:
    """从当日唯一映射结果 AssignedRow 列表构造有序比对读数。

    assigned 已按 CSV（本地时间）顺序给出，且 UTC 严格递增，即时间顺序。
    """
    return [
        DayReading(
            seq=r.seq,
            clock=r.local_time.strftime("%H:%M"),
            fold=r.fold,
            reading=r.reading,
        )
        for r in assigned
    ]


def previous_day_readings(rows: list) -> list[DayReading]:
    """从前日已发布批次的 readings 行构造有序比对读数。

    rows 为 get_published_batch 返回的读数（已按 utc_time 升序），
    每行含 seq / local_time / reading / fold。
    """
    result: list[DayReading] = []
    for r in rows:
        # local_time 以 naive ISO 存储（YYYY-MM-DDTHH:MM:SS）
        clock = datetime.fromisoformat(r["local_time"]).strftime("%H:%M")
        result.append(
            DayReading(
                seq=r["seq"],
                clock=clock,
                fold=int(r["fold"]),
                reading=float(r["reading"]),
            )
        )
    return result


def find_duplicate_runs(
    current: list[DayReading], previous: list[DayReading]
) -> list[MatchedRun]:
    """求两天读数中所有满足阈值的连续匹配段（最长公共子串 DP）。

    dp[i][j]：以 current[i-1] 与 previous[j-1] 为共同右端点的连续匹配长度。
    匹配条件：(钟点, fold, reading) 三元组完全相等。
    只回收长度 >= MIN_MATCH_RUN 的极大段，再用不同读数个数二次过滤。
    """
    n, m = len(current), len(previous)
    cur_keys = [reading_key(d.clock, d.fold, d.reading) for d in current]
    prev_keys = [reading_key(d.clock, d.fold, d.reading) for d in previous]

    # 滚动数组节省内存（两日各至多 500 行，整体仍很小）
    prev_dp = [0] * (m + 1)
    runs: list[MatchedRun] = []

    for i in range(1, n + 1):
        cur_dp = [0] * (m + 1)
        for j in range(1, m + 1):
            if cur_keys[i - 1] == prev_keys[j - 1]:
                length = prev_dp[j - 1] + 1
                cur_dp[j] = length
                # 仅在极大段的右端点收口：下一位置不再延续该匹配
                next_continues = (
                    i < n
                    and j < m
                    and cur_keys[i] == prev_keys[j]
                )
                if length >= MIN_MATCH_RUN and not next_continues:
                    cur_slice = current[i - length : i]
                    prev_slice = previous[j - length : j]
                    distinct = len({d.reading for d in cur_slice})
                    if distinct >= MIN_DISTINCT_READINGS:
                        runs.append(
                            MatchedRun(
                                length=length,
                                distinct_readings=distinct,
                                current_seqs=[d.seq for d in cur_slice],
                                previous_seqs=[d.seq for d in prev_slice],
                            )
                        )
        prev_dp = cur_dp

    return runs


def detect_reupload(
    current: list[DayReading], previous: list[DayReading]
) -> MatchedRun | None:
    """存在任一满足阈值的连续匹配段则返回其中一段，否则 None。"""
    runs = find_duplicate_runs(current, previous)
    return runs[0] if runs else None
