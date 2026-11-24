"""FastAPI application factory and lifespan wiring."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db
from .api import router

API_DESCRIPTION = """
保护区巡护补拍计划的取数接口。数据存放在 SQLite。

**录入与更正**

每观测到一只带标动物，按唯一的 `recognition_id`（识别记录号）录入，
可更正的三项是 `observed_on`（观测日）、`district`（片区）、`ear_tag`（耳标号）。
同一记录号重复提交即更正，不会产生重复记录。
同一片区内同一耳标同一天无论被拍到多少次，统计时只算一天。

**统计口径**

`/stats/districts/{district}` 与 `/stats/districts`（以及等价的 `/stats`）
按起止日（含首尾两天）返回两项个体数：

* `one_day_count`：区间内只在一天被拍到的耳标个体数；
* `multi_day_count`：区间内被拍到至少两天的耳标个体数。

统计只读取按「片区 / 耳标」预聚合的汇总记录（每个记录内的天数列表已在
写入时按天去重），不读取识别明细、不在查询时逐日归并，
单次查询最多访问 1024 条汇总记录。

**一致性**

明细与汇总在同一事务内生效。查询在单个读取快照中进行，
明细与汇总总是来自同一个已提交状态；保存失败时整体回滚，仍能查到原有数字。
响应的 `state_version` 标明本次读写对应的已提交状态版本。

**跨区同耳标勘误**

`POST /errata/ear-tag-batches` 处理两个互不相通片区在同一天出现相同耳标的误读
复核：提交观测日、两个片区、当天两区内至多 16 条识别记录及每条 1～3 个备选耳标，
只改耳标。服务按「改写记录数最少」求解：唯一最优即落库并返回逐条新耳标与复拍汇总；
并列最优返回两套方案、无解返回冲突，这两种情况都不写库；求解期间普通录入与更正
不受影响，落库前发现数据已变则返回重试提示。
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="保护区巡护补拍取数接口",
        description=API_DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(router)

    @app.get("/health", tags=["运维"], summary="健康检查")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
