# 楼宇热量表 UTC 入仓服务（America/New_York）

把纽约本地时钟导出的每日热量表 CSV，转换为**可靠的 UTC 整批数据**入 SQLite。
基于 FastAPI + SQLite + Python 标准库 `zoneinfo`，提供 Docker Compose 一键启动与 `/docs` 操作入口。

## 1. 核心规则

纽约时区（`America/New_York`）每年两次夏令时切换，本地挂钟时间与 UTC 不是一一对应：

| 场景 | 本地钟点 | 处理方式 |
|---|---|---|
| 普通钟点 | 唯一 UTC 映射 | 直接映射 |
| **春季跳变（缺口 gap）** | 02:00→03:00，`02:00–02:59` 物理上不存在 | **整日拒绝发布**，返回原因与涉及序号 |
| **秋季回拨（重复 fold）** | 02:00 调回 01:00，`01:00–01:59` 出现两次 | 每个重复钟点有两个 UTC 候选，须整批求解 |

整批发布判定（`app/tzmap.py`）：

1. 任一行落在春季缺口 → `SPRING_GAP_NONEXISTENT_LOCAL_TIME`，拒绝；
2. 对每行建立 UTC 候选（普通行 1 个、回拨行 2 个），用两遍动态规划求
   **「UTC 严格递增」的完整分配路径数**（计数在 2 处截断，只需区分 0 / 1 / 多）；
3. 路径数 **=0** → `NO_UNIQUE_MAPPING_NO_SOLUTION`（无解，例如本地时间倒流、普通钟点重复）；
4. 路径数 **>1** → `NO_UNIQUE_MAPPING_MULTIPLE_SOLUTIONS`（多解，回拨钟点无法定档）；
5. 路径数 **恰好 1** → 发布整日批次，并重建每行唯一的 UTC 时间。

发布与保留语义：

* **映射失败不写库**，该（表号, 日期）此前发布的批次原样保留，响应中
  `retained_previous_batch=true` 并带回被保留批次信息；
* **成功重传在单事务内完整替换**该日旧批次（先删旧批次与读数，再插新批次），
  查询永远只返回当前同一个完整批次的读数，不会出现新旧混合；
* 批次与读数通过外键 `batch_id` 绑定，`UNIQUE(meter_id, local_date)` 保证每日至多一个已发布批次。

### 跨日重传拦截（换日期重复上传）

网关把**前一天的读数换个日期再次上传**时，CSV 与夏令时映射都合法，但数仓会收到
重复数据。因此当日批次在通过上述 UTC 唯一映射之后，还要与**同一表前一纽约本地日**
（按 `America/New_York` 本地日历 `local_date - 1`，与 UTC 跨日无关）的已发布批次比对
（`app/dedup.py`）：

1. 取两日读数的身份三元组 **（本地挂钟钟点 HH:MM、回拨档位 `fold`、读数 `reading`）**；
   普通钟点 `fold=0`，秋季回拨行 `fold=0` 为第一次经过（EDT）、`fold=1` 为回拨后第二次
   经过（EST），档位随发布结果持久化，旧库自动迁移回填；
2. 以最长公共子串 DP 求两日时间序列上的连续匹配段：三元组逐条相等且两侧同时连续；
3. 存在 **连续至少 5 条** 的匹配段，且段内含 **至少 3 个不同读数**
   （避免表停走、恒定值误判）→ 拒绝整批，错误码 `DUPLICATE_OF_PREVIOUS_DAY_BATCH`，
   返回当日涉及序号 `involved_seqs`、前日批次 ID `previous_batch_id`、
   前日日期及前日对应序号 `previous_involved_seqs`、匹配段长度与不同读数个数；
4. **没有前日已发布批次则不比对**，完全沿用原有发布规则；只比对紧邻的前一本地日，
   更早的日期不参与；
5. 被拒绝时当日原已发布批次原样保留（`retained_previous_batch`），既有上传、查询、
   春季缺口/无解/多解拒绝规则全部继续有效。

**并发一致性（多实例）**：跨日比对与当日发布在**同一个 `BEGIN IMMEDIATE` 写事务**内
完成。事务一进入即取得 SQLite 写锁，持锁期间其他连接无法替换任何批次；另一实例重传
前日批次时会在写锁处阻塞，直到本事务提交或回滚。因此比对所依据的前日批次与发布时刻
已提交的版本必然是同一个，不会出现“按过期版本比对后发布”。写事务通过
`busy_timeout=30s` 串行化，锁等待不会误判。

## 2. CSV 格式

固定三列表头，数据行 **1–500 行**：

```csv
seq,local_time,reading
1,00:00,100.0
2,00:01:101.5
```

* `seq`：正整数，**全表严格递增**；
* `local_time`：分钟级（秒必须为 0）。支持 `HH:MM`、`HH:MM:SS`，或完整 ISO
  （`YYYY-MM-DDTHH:MM:SS`，日期必须等于路径参数 `local_date`）；
* `reading`：有限浮点数，`NaN`/`Infinity` 一律拒绝；
* 编码 UTF-8（兼容带 BOM），表头/列数不符、空文件等均返回 400。

## 3. 启动（Docker Compose）

```bash
docker compose up --build
# 服务：http://localhost:8000
# Swagger 操作入口：http://localhost:8000/docs
# OpenAPI：http://localhost:8000/openapi.json
# 健康检查：http://localhost:8000/healthz
```

SQLite 数据文件位于容器内 `/data/heatmeter.db`，由命名卷 `heatmeter-data` 持久化；
镜像内置 `tzdata`，保证 `zoneinfo` 在精简基础镜像中可用。

本地无 Docker 时（需 Python 3.11+）：

```bash
pip install -r requirements.txt
HEATMETER_DB=./heatmeter.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 4. API 一览

### 上传（发布/重传）

```bash
curl -X POST "http://localhost:8000/meters/M-001/dates/2026-11-01/batches" \
  -F "file=@examples/readings_fall_fold_day.csv;type=text/csv"
```

成功 `200`：

```json
{
  "status": "published",
  "meter_id": "M-001",
  "local_date": "2026-11-01",
  "batch_id": 4,
  "row_count": 6,
  "published_at": "2026-11-14T23:03:58+00:00",
  "replaced_batch_id": 2,
  "previous_published_at": "2026-11-14T22:10:00+00:00",
  "ambiguous_seq": [2, 3, 4, 5]
}
```

失败 `422`（不覆盖旧数据）：

```json
{
  "detail": {
    "error_code": "NO_UNIQUE_MAPPING_MULTIPLE_SOLUTIONS",
    "meter_id": "M-001",
    "local_date": "2026-11-01",
    "reason": "秋季回拨的重复钟点存在歧义，且整批约束下有多个合法 UTC 递增映射，映射不唯一，按规则不发布",
    "involved_seqs": [1, 2],
    "retained_previous_batch": true,
    "retained_batch": {"batch_id": 4, "published_at": "...", "row_count": 6}
  }
}
```

错误码：

| HTTP | error_code | 含义 |
|---|---|---|
| 400 | `CSV_VALIDATION_FAILED` | CSV 结构/字段校验失败（含具体物理行号） |
| 422 | `SPRING_GAP_NONEXISTENT_LOCAL_TIME` | 含春季不存在的钟点，`involved_seqs` 为缺口行 |
| 422 | `NO_UNIQUE_MAPPING_NO_SOLUTION` | UTC 严格递增约束下无解 |
| 422 | `NO_UNIQUE_MAPPING_MULTIPLE_SOLUTIONS` | 约束下多解，回拨钟点无法唯一确定 |
| 422 | `DUPLICATE_OF_PREVIOUS_DAY_BATCH` | 与前一纽约本地日已发布批次存在连续 ≥5 条同钟点、同 `fold`、同读数（含 ≥3 个不同读数），判定为换日期重传；带 `previous_batch_id` 与双方涉及序号 |
| 404 | `NO_PUBLISHED_BATCH` | 该日尚无已发布批次 |

跨日重传拒绝 `422` 响应示例：

```json
{
  "detail": {
    "error_code": "DUPLICATE_OF_PREVIOUS_DAY_BATCH",
    "meter_id": "M-001",
    "local_date": "2026-06-16",
    "reason": "与同一表前一纽约本地日 2026-06-15 的已发布批次（batch_id=12）存在 5 条连续读数在相同本地钟点与回拨档位上逐条相等，且含 5 个不同读数（阈值：连续≥5 条、不同读数≥3 个），判定为前日读数换日期重传，按规则拒绝整批",
    "involved_seqs": [1, 2, 3, 4, 5],
    "retained_previous_batch": false,
    "previous_local_date": "2026-06-15",
    "previous_batch_id": 12,
    "matched_run_length": 5,
    "matched_distinct_readings": 5,
    "previous_involved_seqs": [1, 2, 3, 4, 5]
  }
}
```

复现（同一份 CSV 换次日日期再传即被拦截）：

```bash
curl -X POST "http://localhost:8000/meters/M-001/dates/2026-06-15/batches" \
  -F "file=@examples/readings_normal_day.csv;type=text/csv"   # 200 发布
curl -X POST "http://localhost:8000/meters/M-001/dates/2026-06-16/batches" \
  -F "file=@examples/readings_normal_day.csv;type=text/csv"   # 422 拒绝
```

### 查询该日 UTC 读数

```bash
curl "http://localhost:8000/meters/M-001/dates/2026-11-01/readings"
```

读数按 UTC 升序返回，全部来自同一个 `batch_id`；`mapping` 字段标记
`normal`（普通钟点）或 `ambiguous`（秋季回拨，已按唯一解定档），`fold` 字段标记
回拨档位（`0`=第一次经过/普通钟点，`1`=回拨后第二次经过），也是跨日重传比对的档位依据。

### 其他

* `GET /meters/{meter_id}/dates`：列出该表所有已发布日期与批次；
* `GET /healthz`：健康检查（已配置容器 healthcheck）。

## 5. 目录结构

```
app/
  main.py      FastAPI 路由与错误响应（锁内跨日比对 + 发布）
  tzmap.py     本地→UTC 候选识别 + 整批唯一解 DP 求解（含 fold 档位）
  csvparse.py  CSV 严格解析与校验
  dedup.py     跨日重传检测：(钟点, fold, 读数) 最长公共子串连续匹配
  db.py        SQLite 批次/读数表、fold 迁移、BEGIN IMMEDIATE 事务化完整替换
tests/         49 项单元与端到端测试（春秋切换、无解/多解、保留/替换、CSV 边界、
               跨日重传阈值/档位/隔离、并发锁、旧库迁移）
examples/      普通日、秋季回拨日、春季缺口（被拒）三个样例
Dockerfile / docker-compose.yml / requirements.txt
```

## 6. 测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest
```

覆盖要点：春/秋令时真实边界（基于 `zoneinfo` 实测而非硬编码偏移）、
缺口拒绝序号、回拨唯一/多解、倒流无解、500 行上限、NaN 拒绝、
失败保留旧批次、成功重传物理清除旧数据、整批查询一致性；
跨日重传的 5 连/3 不同读数阈值、fold 档位区分、按表号隔离、仅比对紧邻前一本地日、
拒绝保留当日批次、`BEGIN IMMEDIATE` 锁下不按过期前日版本决策、无 fold 列旧库自动回填。
