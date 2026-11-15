"""本地时间 -> UTC 的整批映射核心。

规则（America/New_York，纽约本地时钟）：
* 普通钟点：本地时间只有一个 UTC 映射；
* 春季跳变（不存在的钟点，gap）：该本地时间物理上从未发生，整批拒绝；
* 秋季回拨（重复钟点，fold）：同一本地时间对应两个 UTC 候选，
  必须结合“整批 UTC 严格递增”的约束求解；
* 整批映射要求 **存在且仅存在一个** 满足约束的解：
  - 无解（NO_SOLUTION）或多解（MULTIPLE_SOLUTIONS）均不发布；
  - 唯一解才允许发布整日批次。

求解用两遍动态规划（计数在 2 处截断，只区分 0 / 1 / >1）：
* forward[i][c]：到达第 i 行第 c 个候选的严格递增路径数；
* backward[i][c]：从该候选出发到末尾的严格递增路径数。
唯一性即总路径数 == 1，且唯一解可由“计数恰为 1 的候选”直接重建。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")
NEW_YORK = ZoneInfo("America/New_York")


class RowStatus(str, Enum):
    NORMAL = "normal"        # 普通钟点，唯一映射
    AMBIGUOUS = "ambiguous"  # 秋季回拨，两个候选
    GAP = "gap"              # 春季跳变，不存在的钟点


class BatchOutcome(str, Enum):
    UNIQUE = "unique"                  # 唯一解，可发布
    GAP_REJECTED = "gap_rejected"      # 含春季不存在钟点
    NO_SOLUTION = "no_solution"        # 约束下无解
    MULTIPLE_SOLUTIONS = "multiple_solutions"  # 约束下多解


@dataclass(frozen=True)
class RowInput:
    """一行已解析的本地读数（序号与本地时间已通过格式校验）。"""

    seq: int
    local_time: datetime  # naive，分钟精度，日期归属已确认
    reading: float


@dataclass(frozen=True)
class RowMapping:
    seq: int
    local_time: datetime
    reading: float
    status: RowStatus
    candidates_utc: tuple[datetime, ...]  # aware UTC，升序；gap 为空


@dataclass(frozen=True)
class AssignedRow:
    seq: int
    local_time: datetime
    reading: float
    status: RowStatus
    utc_time: datetime
    # 回拨档位：普通钟点固定 0；秋季回拨行等于候选下标，
    # 0=第一次经过（EDT，fold=0），1=回拨后第二次经过（EST，fold=1）
    fold: int


@dataclass
class BatchMappingResult:
    outcome: BatchOutcome
    rows: list[RowMapping] = field(default_factory=list)
    assigned: list[AssignedRow] = field(default_factory=list)
    # 拒绝原因（中文，面向数据工程师）
    reason: str | None = None
    # 涉及序号
    involved_seqs: list[int] = field(default_factory=list)
    total_solutions: int = 0  # 0 / 1 / 2（2 表示“多个”，被截断）


def local_candidates(local_naive: datetime) -> tuple[RowStatus, tuple[datetime, ...]]:
    """计算一个纽约本地 naive 时间的 UTC 候选。

    依据 PEP 495 fold 语义及 zoneinfo 的实际归位规则：
    * 两档 fold 的 UTC 偏移相同 -> 普通钟点（唯一映射）；
    * 偏移不同且 fold=0 的 UTC **更早** -> 秋季回拨（重复钟点，两个候选，升序）；
    * 偏移不同且 fold=1 的 UTC 更早 -> 春季跳变（不存在的钟点，无候选）。
      （zoneinfo 对缺口时间：fold=0 归位到跳变后偏移、fold=1 归位到跳变前偏移。）
    """
    fold0 = local_naive.replace(tzinfo=NEW_YORK, fold=0)
    fold1 = local_naive.replace(tzinfo=NEW_YORK, fold=1)

    if fold0.utcoffset() == fold1.utcoffset():
        return RowStatus.NORMAL, (fold0.astimezone(UTC),)

    u0 = fold0.astimezone(UTC)
    u1 = fold1.astimezone(UTC)
    if u0 < u1:
        # 秋季回拨：fold=0=EDT（较早），fold=1=EST（较晚）
        return RowStatus.AMBIGUOUS, (u0, u1)

    # 春季缺口：该本地挂钟时间物理上不存在
    return RowStatus.GAP, ()


def _cap_add(a: int, b: int) -> int:
    """路径计数加法，超过 1 截断到 2（只需区分 0/1/多）。"""
    return min(2, a + b)


def solve_batch(rows: list[RowInput]) -> BatchMappingResult:
    """对整批行求解 UTC 严格递增映射，判定可发布性。"""
    mappings: list[RowMapping] = []
    gap_seqs: list[int] = []

    for r in rows:
        status, candidates = local_candidates(r.local_time)
        mappings.append(
            RowMapping(
                seq=r.seq,
                local_time=r.local_time,
                reading=r.reading,
                status=status,
                candidates_utc=candidates,
            )
        )
        if status is RowStatus.GAP:
            gap_seqs.append(r.seq)

    if gap_seqs:
        return BatchMappingResult(
            outcome=BatchOutcome.GAP_REJECTED,
            rows=mappings,
            reason=(
                "春季夏令时跳变产生的不存在钟点（纽约本地 02:00–02:59 缺口），"
                "该本地时间从未实际发生，无法映射到 UTC"
            ),
            involved_seqs=gap_seqs,
            total_solutions=0,
        )

    n = len(mappings)
    # forward[i][c]：到达 (i, c) 的严格递增路径数（0/1/2）
    forward: list[list[int]] = []
    for i, m in enumerate(mappings):
        cur: list[int] = []
        for c in range(len(m.candidates_utc)):
            if i == 0:
                cur.append(1)
                continue
            ways = 0
            ts = m.candidates_utc[c]
            prev = mappings[i - 1]
            for pc, pts in enumerate(prev.candidates_utc):
                if pts < ts:
                    ways = _cap_add(ways, forward[i - 1][pc])
            cur.append(ways)
        forward.append(cur)

    # backward[i][c]：从 (i, c) 走到末行的严格递增路径数
    backward: list[list[int]] = [[0] * len(m.candidates_utc) for m in mappings]
    for i in range(n - 1, -1, -1):
        m = mappings[i]
        for c in range(len(m.candidates_utc)):
            if i == n - 1:
                backward[i][c] = 1
                continue
            ways = 0
            ts = m.candidates_utc[c]
            nxt = mappings[i + 1]
            for nc, nts in enumerate(nxt.candidates_utc):
                if ts < nts:
                    ways = _cap_add(ways, backward[i + 1][nc])
            backward[i][c] = ways

    total = 0
    for c in range(len(mappings[-1].candidates_utc)):
        total = _cap_add(total, forward[n - 1][c])

    if total == 0:
        involved = [m.seq for i, m in enumerate(mappings) if sum(forward[i]) == 0]
        return BatchMappingResult(
            outcome=BatchOutcome.NO_SOLUTION,
            rows=mappings,
            reason=(
                "在“整批 UTC 严格递增”约束下无解：存在无法由任何前置 UTC 时刻"
                "严格递增到达的行（本地时间倒流、重复普通钟点或回拨候选均不满足约束）"
            ),
            involved_seqs=involved,
            total_solutions=0,
        )

    if total == 2:
        involved = [
            m.seq
            for i, m in enumerate(mappings)
            if m.status is RowStatus.AMBIGUOUS
            and any(
                forward[i][c] >= 1 and backward[i][c] >= 1
                for c in range(len(m.candidates_utc))
            )
        ]
        return BatchMappingResult(
            outcome=BatchOutcome.MULTIPLE_SOLUTIONS,
            rows=mappings,
            reason=(
                "秋季回拨的重复钟点存在歧义，且整批约束下有多个合法 UTC 递增映射，"
                "映射不唯一，按规则不发布"
            ),
            involved_seqs=involved,
            total_solutions=2,
        )

    # 唯一解：每行恰有一个候选满足 forward==1 且 backward==1
    assigned: list[AssignedRow] = []
    for i, m in enumerate(mappings):
        chosen = [
            c
            for c in range(len(m.candidates_utc))
            if forward[i][c] == 1 and backward[i][c] == 1
        ]
        assert len(chosen) == 1, "唯一解判定与重建不一致"
        c = chosen[0]
        assigned.append(
            AssignedRow(
                seq=m.seq,
                local_time=m.local_time,
                reading=m.reading,
                status=m.status,
                utc_time=m.candidates_utc[c],
                # 普通钟点只有一个候选（c=0）；回拨行 c 即 fold 档位
                fold=c,
            )
        )

    return BatchMappingResult(
        outcome=BatchOutcome.UNIQUE,
        rows=mappings,
        assigned=assigned,
        reason=None,
        involved_seqs=[],
        total_solutions=1,
    )
