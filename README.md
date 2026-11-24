# 保护区巡护补拍取数接口

按唯一的识别记录号录入带标动物的观测，并按片区与起止日统计
「只在一天被拍到」和「至少两天被拍到」的个体数，供巡护组排补拍计划。

## 启动

### docker compose（推荐）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>，交互式文档在 <http://localhost:8000/docs>。
SQLite 文件位于命名卷 `resighting-data` 的 `/data/resighting.db`，重启容器后数据保留。

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

默认数据库为 `data/resighting.db`，可用环境变量 `DB_PATH` 覆盖。

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/observations` | 按 `recognition_id` 录入；同一记录号再次提交即更正三项属性 |
| `PUT` | `/observations/{recognition_id}` | 同上，记录号在路径中 |
| `PATCH` | `/observations/{recognition_id}` | 只更正提供的字段 |
| `GET` | `/observations/{recognition_id}` | 读取单条记录 |
| `GET` | `/observations` | 分页明细，可按片区 / 耳标 / 起止日筛选 |
| `GET` | `/stats/districts/{district}` | 单片区统计 |
| `GET` | `/stats/districts` | 全部片区统计（可用 `district` 只取一个） |
| `POST` | `/errata/ear-tag-batches` | 跨区同耳标勘误批：按最少改动求解，唯一最优时只改耳标落库 |
| `GET` | `/health` | 健康检查 |

录入的请求体：

```json
{
  "recognition_id": "REC-0001",
  "observed_on": "2026-03-01",
  "district": "北沟片区",
  "ear_tag": "ET-1024"
}
```

可更正的三项是 `observed_on`、`district`、`ear_tag`；`recognition_id` 是唯一键，不可更正。

统计返回：

```json
{
  "district": "北沟片区",
  "start_date": "2026-03-01",
  "end_date": "2026-03-31",
  "one_day_count": 12,
  "multi_day_count": 5,
  "total_individuals": 17,
  "summary_rows_used": 23,
  "state_version": 41
}
```

起止日都包含。同一片区内同一耳标同一天被拍到多次只算一天。

## 跨区同耳标勘误

两处互不相通的围栏样区在同一天拍到相同耳标时，巡护员在
`POST /errata/ear-tag-batches` 提交一个勘误批，对误读做复核。该入口同样可在
<http://localhost:8000/docs> 的交互式文档中直接调用。

请求体：

```json
{
  "observed_on": "2026-03-01",
  "districts": ["北沟片区", "南坡片区"],
  "items": [
    {"recognition_id": "REC-0001", "candidate_ear_tags": ["ET-2048", "ET-3072"]},
    {"recognition_id": "REC-0002", "candidate_ear_tags": ["ET-1024"]}
  ]
}
```

约束与口径：

* 两个片区必须互不相同；`items` 为当天两区内 **1～16 个不同识别记录号**，
  每条带 **1～3 个备选耳标**（当前耳标自动作为零改动选项，不必重复填）。
* 记录必须确实存在，且观测日、片区与请求一致，否则返回 422。
* **只改耳标**：观测日与片区保持不变；同区多拍允许，批外记录照常计入占用。
* 整批改完后，本批的原耳标与所有备选耳标，当天都不得在两个片区同时出现。
* 优化目标是**改写记录数最少**（保留原耳标计 0，换成备选计 1）。

四种结果（响应字段 `outcome`）：

| outcome | HTTP | 是否写库 | 含义 |
|---|---|---|---|
| `applied` | 200 | 是 | 唯一最优；返回逐条新耳标、改写数与落库后复拍汇总 |
| `ambiguous` | 200 | 否 | 存在并列最优，返回其中两套方案供人工裁决 |
| `conflict` | 409 | 否 | 无可行赋值，返回冲突原因 |
| `stale` | 409 | 否 | 求解后数据已变（`retryable=true`），按当前数据重新提交即可 |

并发与一致性：

* **求解不持写锁**。勘误在只读快照上完成，求解期间普通录入与更正仍可提交。
* 落库在一个短写事务内先复检：待改记录的值若已变化，或批外记录改变了相关
  耳标当天在两区的占用（增加或减少都算），返回 `stale` 提示重试；无关写入
  （其他片区 / 其他耳标 / 其他日期）不影响提交。
* 取数与复核只读待改明细（按记录号最多 16 行）和两区相关耳标的汇总记录，
  不扫描无关数据；明细、汇总与状态版本在同一事务提交，保存失败整体回滚。
* 零改动的唯一最优不开启写事务，状态版本不变。

## 实现要点

* **按天去重发生在写入时。** 汇总表 `tag_summaries` 每个
  `(片区, 耳标)` 一行，`day_counts` 是以日期为键的 JSON 数组，
  天数与识别次数分开记录。同一天多次拍摄只增加当天计数，不会产生新的天。
* **统计只读汇总。** 统计 SQL 不访问 `observations`，也不做 `GROUP BY`
  或逐日归并；区间内的天数是在已排序的日期列表上做两次二分查找。
* **汇总记录有上限。** 单次统计最多读取 1024 条汇总记录；
  超过时返回 422 并提示缩小范围，而不是返回截断的结果。
* **更正只读新旧范围。** 更正按唯一键读取 1 条明细，另读至多 2 条汇总记录
  （旧片区/耳标、新片区/耳标），读取行数与耳标的历史天数无关。
* **事务一致。** 明细、汇总与状态版本在同一写事务内提交，失败整体回滚；
  查询在一个读取快照内完成，明细与汇总始终来自同一已提交状态。
  响应的 `state_version` 可用来判断读写对应的是哪一次已提交状态。

## 测试

```bash
pytest
```

覆盖：同日多拍、改期后跨天、起止日包含、片区隔离、路由级更正、
1024 条上限、统计不读明细、更正读取量不随历史增长、失败保存回滚、
并发写入期间读到旧的已提交状态；勘误批的求解器穷举差分（唯一 / 并列 /
无解、最少改动、同区多拍、批外占用）、耳标互换落库、求解期间补录、
stale 复检（记录变更与批外占用增减）、无关写入不拦截、读取边界与回滚。
