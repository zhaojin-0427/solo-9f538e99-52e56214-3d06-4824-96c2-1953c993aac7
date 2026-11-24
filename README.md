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
并发写入期间读到旧的已提交状态。
