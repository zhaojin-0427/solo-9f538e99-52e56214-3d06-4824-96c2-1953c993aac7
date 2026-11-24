"""HTTP API for the patrol resighting service.

All endpoints are usable from the auto-generated ``/docs`` page: request
models carry examples and every field is described.

Endpoint map
------------
Writes (records are keyed by the unique recognition record number):

* ``POST /observations``                      create or correct a full record
* ``PUT  /observations/{recognition_id}``     same, id is in the path
* ``PATCH /observations/{recognition_id}``    correct just the changed fields

Reads:

* ``GET /observations``                       paged detail listing
* ``GET /observations/{recognition_id}``      one record
* ``GET /stats/districts/{district}``         one-day / multi-day counts
* ``GET /stats/districts``                    the same for every district
* ``GET /stats``                              alias of ``/stats/districts``

Request fields also accept a few common alternative names (``date``, ``area``,
``tag``, ``record_id`` …) so that a slightly different naming convention does
not cause a 422.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Response, status
from pydantic import AliasChoices

from . import errata as errata_logic, repository
from .models import (
    AllDistrictStats,
    DistrictStats,
    EarTagErrataResponse,
    ErrataBatchInput,
    ErrataLineResult,
    ErrataPlanResult,
    ErrataRecaptureRow,
    ObservationCorrection,
    ObservationDetail,
    ObservationInput,
    ObservationList,
    ObservationRecord,
    ObservationValues,
    ObservationWriteResult,
)

router = APIRouter()

RecognitionId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=256,
        description="唯一的识别记录号",
        examples=["REC-0001"],
    ),
]

StartDate = Annotated[
    date,
    Query(
        description="起始日（含）",
        validation_alias=AliasChoices("start_date", "start", "from", "from_date"),
    ),
]
EndDate = Annotated[
    date,
    Query(
        description="结束日（含）",
        validation_alias=AliasChoices("end_date", "end", "to", "to_date"),
    ),
]
OptionalStartDate = Annotated[
    date | None,
    Query(
        description="起始日（含）",
        validation_alias=AliasChoices("start_date", "start", "from", "from_date"),
    ),
]
OptionalEndDate = Annotated[
    date | None,
    Query(
        description="结束日（含）",
        validation_alias=AliasChoices("end_date", "end", "to", "to_date"),
    ),
]


def _write_response(result: tuple[str, dict, int]) -> ObservationWriteResult:
    action, record, version = result
    return ObservationWriteResult(
        action=action, record=ObservationRecord(**record), state_version=version
    )


def _not_found(recognition_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"识别记录号 {recognition_id} 不存在",
    )


# ---------------------------------------------------------------------------
# Observation entry / correction
# ---------------------------------------------------------------------------


@router.post(
    "/observations",
    response_model=ObservationWriteResult,
    summary="按识别记录号录入观测（重复提交即更正）",
    description=(
        "以 `recognition_id` 为唯一键录入一条识别记录。"
        "同一记录号再次提交时，更正 `observed_on`、`district`、`ear_tag` 三项。"
        "明细与汇总在同一事务内生效；保存失败时整体回滚，原有统计不变。"
    ),
    responses={422: {"description": "请求内容不合法"}},
)
def upsert_observation(payload: ObservationInput) -> ObservationWriteResult:
    return _write_response(
        repository.put_observation(
            recognition_id=payload.recognition_id,
            observed_on=payload.observed_on,
            district=payload.district,
            ear_tag=payload.ear_tag,
        )
    )


@router.put(
    "/observations/{recognition_id}",
    response_model=ObservationWriteResult,
    summary="按路径中的识别记录号录入或更正观测",
    description="与 `POST /observations` 等价，记录号放在路径中。",
)
def put_observation(
    recognition_id: RecognitionId, payload: ObservationValues
) -> ObservationWriteResult:
    return _write_response(
        repository.put_observation(
            recognition_id=recognition_id,
            observed_on=payload.observed_on,
            district=payload.district,
            ear_tag=payload.ear_tag,
        )
    )


@router.patch(
    "/observations/{recognition_id}",
    response_model=ObservationWriteResult,
    summary="更正观测日、片区、耳标号（可只改其中一项）",
    description=(
        "只更正提供的字段，未提供的字段保持原值。识别记录号本身不可更正。"
        "更正只读取该记录本身及新旧片区/耳标对应的汇总记录，"
        "读取行数与耳标的历史天数无关。"
    ),
    responses={
        404: {"description": "识别记录号不存在"},
        422: {"description": "未提供任何可更正字段或字段不合法"},
    },
)
def correct_observation(
    recognition_id: RecognitionId, payload: ObservationCorrection
) -> ObservationWriteResult:
    try:
        result = repository.correct_observation(
            recognition_id, payload.model_dump(exclude_none=True)
        )
    except repository.NotFoundError:
        raise _not_found(recognition_id)
    return _write_response(result)


# ---------------------------------------------------------------------------
# Detail reads
# ---------------------------------------------------------------------------


@router.get(
    "/observations/{recognition_id}",
    response_model=ObservationDetail,
    summary="读取单条识别记录",
    responses={404: {"description": "识别记录号不存在"}},
)
def get_observation(recognition_id: RecognitionId) -> ObservationDetail:
    try:
        return ObservationDetail(**repository.get_observation(recognition_id))
    except repository.NotFoundError:
        raise _not_found(recognition_id)


@router.get(
    "/observations",
    response_model=ObservationList,
    summary="分页查询识别明细（可选按片区、耳标、日期筛选）",
    description="该接口用于核对明细；统计接口不读这里的数据。",
)
def list_observations(
    district: Annotated[str | None, Query(description="片区")] = None,
    ear_tag: Annotated[str | None, Query(description="耳标号")] = None,
    recognition_id: Annotated[str | None, Query(description="识别记录号")] = None,
    start_date: OptionalStartDate = None,
    end_date: OptionalEndDate = None,
    limit: Annotated[int, Query(ge=1, le=1000, description="每页条数")] = 100,
    offset: Annotated[int, Query(ge=0, description="偏移量")] = 0,
) -> ObservationList:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date 不能晚于 end_date")
    return ObservationList(
        **repository.list_observations(
            district=district,
            ear_tag=ear_tag,
            recognition_id=recognition_id,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )
    )


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _stats_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (repository.SummaryLimitExceeded, repository.InvalidRangeError)):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


@router.get(
    "/stats/districts/{district}",
    response_model=DistrictStats,
    summary="按片区与起止日统计只在一天 / 至少两天被拍到的个体数",
    description=(
        "起止日均包含。同一片区内同一耳标同一天被拍到多次只算一天。"
        "统计只读取汇总记录（每个 片区/耳标 一条，天内次数已在写入时归并），"
        "不读取识别明细，也不在查询时逐日归并；单次查询最多访问 1024 条汇总记录，"
        "超出时返回 422 而不是截断结果。"
    ),
    responses={422: {"description": "日期范围不合法或超出汇总记录上限"}},
)
def district_stats(
    district: Annotated[str, Path(min_length=1, description="片区")],
    start_date: StartDate,
    end_date: EndDate,
) -> DistrictStats:
    try:
        result = repository.district_stats(district, start_date, end_date)
    except (repository.SummaryLimitExceeded, repository.InvalidRangeError) as exc:
        raise _stats_http_error(exc)
    return DistrictStats(**result)


@router.get(
    "/stats/districts",
    response_model=AllDistrictStats,
    summary="按起止日统计所有片区的只在一天 / 至少两天个体数",
    description=(
        "起止日均包含。逐片区返回两项个体数。所有片区合计最多访问 "
        "1024 条汇总记录，超出时返回 422。"
        "可用 `district` 参数只保留一个片区，此时与按片区接口口径一致。"
    ),
    responses={422: {"description": "日期范围不合法或超出汇总记录上限"}},
)
def all_district_stats(
    start_date: StartDate,
    end_date: EndDate,
    district: Annotated[str | None, Query(description="仅统计该片区（可选）")] = None,
) -> AllDistrictStats:
    try:
        result = repository.all_district_stats(start_date, end_date, district=district)
    except (repository.SummaryLimitExceeded, repository.InvalidRangeError) as exc:
        raise _stats_http_error(exc)
    return AllDistrictStats(**result)


@router.get(
    "/stats",
    response_model=AllDistrictStats,
    summary="按起止日查数（可用 district 限定片区）",
    description=(
        "`/stats/districts` 的等价入口：起止日均包含，"
        "可只传一个片区，也可查询全部片区。"
    ),
    responses={422: {"description": "日期范围不合法或超出汇总记录上限"}},
)
def stats(
    start_date: StartDate,
    end_date: EndDate,
    district: Annotated[str | None, Query(description="片区（可选）")] = None,
) -> AllDistrictStats:
    try:
        result = repository.all_district_stats(start_date, end_date, district=district)
    except (repository.SummaryLimitExceeded, repository.InvalidRangeError) as exc:
        raise _stats_http_error(exc)
    return AllDistrictStats(**result)


# ---------------------------------------------------------------------------
# Cross-district same-tag errata (misread review)
# ---------------------------------------------------------------------------


def _plan_result(plan) -> ErrataPlanResult:
    return ErrataPlanResult(
        changed_count=plan.changed_count,
        lines=[
            ErrataLineResult(
                recognition_id=line.recognition_id,
                district=line.district,
                original_ear_tag=line.original_ear_tag,
                new_ear_tag=line.new_ear_tag,
                changed=line.changed,
            )
            for line in plan.lines
        ],
    )


@router.post(
    "/errata/ear-tag-batches",
    response_model=EarTagErrataResponse,
    summary="跨区同耳标勘误批：按最少改动求解，唯一最优时只改耳标落库",
    description=(
        "两处互不相通的围栏样区在同一天拍到相同耳标时，巡护员提交观测日、"
        "两个片区和当天两区内至多 16 个不同识别记录号，每条带 1～3 个备选耳标。"
        "只允许改耳标，观测日与片区不变；同区多拍允许。\n\n"
        "求解在只读快照上进行，不持写锁，因此求解期间普通录入与更正仍可提交。"
        "优化目标是改写记录数最少：\n\n"
        "* 唯一最优（outcome=`applied`）：逐条新耳标落库，并返回落库后相关耳标"
        "当天在两区的复拍汇总；\n"
        "* 并列最优（outcome=`ambiguous`）：返回其中两套方案，不写库；\n"
        "* 无解（outcome=`conflict`，HTTP 409）：返回冲突原因，不写库；\n"
        "* 求解后待改记录已变，或批外记录改变了相关耳标当天两区占用"
        "（outcome=`stale`，HTTP 409，retryable=true）：本次不写库，请重试；\n"
        "  无关写入不影响提交。\n\n"
        "整批改完后，本批原耳标与所有备选耳标都不会在当天跨区出现（批外记录照算）。"
    ),
    responses={
        409: {"description": "无解（conflict）或求解后数据已变需重试（stale）"},
        422: {"description": "请求不合法：片区相同、记录缺失、观测日或片区不匹配等"},
    },
)
def submit_ear_tag_errata(
    payload: ErrataBatchInput, http_response: Response
) -> EarTagErrataResponse:
    districts = (payload.districts[0], payload.districts[1])
    items = [
        {
            "recognition_id": item.recognition_id,
            "candidate_ear_tags": item.candidate_ear_tags,
        }
        for item in payload.items
    ]
    try:
        errata_logic.validate_request(payload.observed_on, districts, payload.items)
        snapshot = repository.load_errata_snapshot(
            payload.observed_on, districts, items
        )
    except errata_logic.ErrataValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    outcome = errata_logic.solve_errata(snapshot)
    common = {
        "observed_on": payload.observed_on,
        "districts": list(districts),
        "solved_state_version": snapshot.state_version,
    }

    if outcome.outcome == "conflict":
        http_response.status_code = status.HTTP_409_CONFLICT
        return EarTagErrataResponse(
            outcome="conflict",
            reasons=outcome.reasons,
            **common,
        )

    if outcome.outcome == "ambiguous":
        return EarTagErrataResponse(
            outcome="ambiguous",
            plans=[_plan_result(plan) for plan in outcome.plans],
            **common,
        )

    # Unique optimum.  A zero-change optimum needs no write transaction at all:
    # the database already satisfies the constraint, so the state version stays
    # exactly the solved one.  The recapture summary is a read-only snapshot.
    plan = outcome.plans[0]
    if plan.changed_count == 0:
        return EarTagErrataResponse(
            outcome="applied",
            plans=[_plan_result(plan)],
            recapture_summary=[
                ErrataRecaptureRow(**row)
                for row in repository.recapture_summary_for_snapshot(snapshot)
            ],
            committed_state_version=snapshot.state_version,
            **common,
        )

    try:
        saved = repository.apply_errata_plan(snapshot, plan)
    except errata_logic.ErrataStaleError as exc:
        http_response.status_code = status.HTTP_409_CONFLICT
        return EarTagErrataResponse(
            outcome="stale",
            reasons=[exc.reason],
            retryable=True,
            **common,
        )

    return EarTagErrataResponse(
        outcome="applied",
        plans=[_plan_result(plan)],
        recapture_summary=[
            ErrataRecaptureRow(**row) for row in saved["recapture_summary"]
        ],
        committed_state_version=saved["state_version"],
        **common,
    )
