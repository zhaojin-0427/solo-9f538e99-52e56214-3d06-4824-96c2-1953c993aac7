"""Pydantic request/response models.

The editable observation attributes are always ``observed_on``, ``district``
and ``ear_tag``.  ``recognition_id`` is the stable identity of a record and is
never edited.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)


def _coerce_identifier(value: Any) -> Any:
    """Accept numeric record numbers / ear tags as well as strings.

    Booleans are rejected because ``True`` would otherwise silently become
    ``"True"`` and create a bogus identifier.
    """
    if isinstance(value, bool):
        raise TypeError("必须是字符串或整数形式的标识")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise ValueError("不能为空字符串")
    return value


Identifier = Annotated[
    str,
    BeforeValidator(_coerce_identifier),
    Field(min_length=1, max_length=256),
]


class ObservationValues(BaseModel):
    """The three editable attributes of one identification record."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "observed_on": "2026-03-01",
                    "district": "北沟片区",
                    "ear_tag": "ET-1024",
                }
            ]
        },
    )

    observed_on: date = Field(
        description="观测日（YYYY-MM-DD）",
        validation_alias=AliasChoices(
            "observed_on", "observed_date", "date", "survey_date", "观测日"
        ),
    )
    district: Identifier = Field(
        description="片区",
        validation_alias=AliasChoices("district", "area", "zone", "片区"),
    )
    ear_tag: Identifier = Field(
        description="耳标号",
        validation_alias=AliasChoices("ear_tag", "tag", "ear_tag_no", "耳标号"),
    )


class ObservationInput(ObservationValues):
    """A complete identification record including its unique record number."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "recognition_id": "REC-0001",
                    "observed_on": "2026-03-01",
                    "district": "北沟片区",
                    "ear_tag": "ET-1024",
                }
            ]
        },
    )

    recognition_id: Identifier = Field(
        description="唯一的识别记录号；同一记录号再次提交即更正已有记录",
        validation_alias=AliasChoices(
            "recognition_id", "recognition_record_id", "record_id", "id", "识别记录号"
        ),
    )


class ObservationCorrection(BaseModel):
    """Partial correction of the three editable attributes.

    Omitted attributes keep their current value; at least one must be given.
    The recognition record number itself cannot be corrected.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"observed_on": "2026-03-08"},
                {"district": "南坡片区", "ear_tag": "ET-2048"},
            ]
        },
    )

    observed_on: date | None = Field(
        default=None,
        description="更正后的观测日",
        validation_alias=AliasChoices(
            "observed_on", "observed_date", "date", "survey_date", "观测日"
        ),
    )
    district: Identifier | None = Field(
        default=None,
        description="更正后的片区",
        validation_alias=AliasChoices("district", "area", "zone", "片区"),
    )
    ear_tag: Identifier | None = Field(
        default=None,
        description="更正后的耳标号",
        validation_alias=AliasChoices("ear_tag", "tag", "ear_tag_no", "耳标号"),
    )

    @model_validator(mode="after")
    def at_least_one_field(self) -> ObservationCorrection:
        if self.observed_on is None and self.district is None and self.ear_tag is None:
            raise ValueError("至少需要提供 observed_on、district、ear_tag 中的一项")
        return self


class ObservationRecord(BaseModel):
    recognition_id: str = Field(description="唯一的识别记录号")
    observed_on: date = Field(description="观测日")
    district: str = Field(description="片区")
    ear_tag: str = Field(description="耳标号")


class ObservationWriteResult(BaseModel):
    action: str = Field(description="created、updated 或 unchanged")
    record: ObservationRecord
    state_version: int = Field(description="写入提交后的已提交状态版本号")


class ObservationDetail(ObservationRecord):
    state_version: int = Field(description="本次读取所依据的已提交状态版本号")


class ObservationList(BaseModel):
    total: int = Field(description="符合筛选条件的明细记录数")
    limit: int
    offset: int
    records: list[ObservationRecord]
    state_version: int = Field(description="本次读取所依据的已提交状态版本号")


class DistrictStats(BaseModel):
    district: str
    start_date: date
    end_date: date
    one_day_count: int = Field(
        description="区间内只在一天被拍到的耳标个体数；同片区内同日多次仍算一天"
    )
    multi_day_count: int = Field(description="区间内被拍到至少两天的耳标个体数")
    total_individuals: int = Field(
        description="区间内出现过的耳标个体数，等于前两项之和"
    )
    summary_rows_used: int = Field(
        description="本次统计读取的日汇总记录数；超过 1024 条时拒绝查询"
    )
    state_version: int = Field(description="本次读取所依据的已提交状态版本号")


class DistrictStatsBrief(BaseModel):
    district: str
    start_date: date
    end_date: date
    one_day_count: int = Field(description="该片区只在一天被拍到的耳标个体数")
    multi_day_count: int = Field(description="该片区至少两天被拍到的耳标个体数")
    total_individuals: int = Field(description="该片区出现过的耳标个体数")


class AllDistrictStats(BaseModel):
    start_date: date
    end_date: date
    total_summary_rows_used: int = Field(
        description="所有片区合计读取的日汇总记录数；超过 1024 条时拒绝查询"
    )
    total_one_day_count: int = Field(description="所有片区只一天个体数合计")
    total_multi_day_count: int = Field(description="所有片区至少两天个体数合计")
    districts: list[DistrictStatsBrief]
    state_version: int = Field(description="本次读取所依据的已提交状态版本号")


# ---------------------------------------------------------------------------
# Cross-district same-tag errata batch
# ---------------------------------------------------------------------------


class ErrataItem(BaseModel):
    """One identification record of the errata batch and its candidate tags."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"recognition_id": "REC-0007", "candidate_ear_tags": ["ET-2048", "ET-3072"]}
            ]
        },
    )

    recognition_id: Identifier = Field(
        description="当天在两个片区之一拍到的识别记录号（批内唯一）",
        validation_alias=AliasChoices(
            "recognition_id", "recognition_record_id", "record_id", "id", "识别记录号"
        ),
    )
    candidate_ear_tags: list[Identifier] = Field(
        min_length=1,
        max_length=3,
        description="1～3 个备选耳标号；当前耳标自动作为零改动选项，无需重复填写",
        validation_alias=AliasChoices(
            "candidate_ear_tags", "candidate_tags", "candidates", "备选耳标"
        ),
    )


class ErrataBatchInput(BaseModel):
    """A same-day, two-district ear-tag misread review batch."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "observed_on": "2026-03-01",
                    "districts": ["北沟片区", "南坡片区"],
                    "items": [
                        {"recognition_id": "REC-0001", "candidate_ear_tags": ["ET-2048"]},
                        {
                            "recognition_id": "REC-0002",
                            "candidate_ear_tags": ["ET-1024", "ET-4096"],
                        },
                    ],
                }
            ]
        },
    )

    observed_on: date = Field(
        description="观测日（YYYY-MM-DD）；批内记录当天必须确实位于这两个片区",
        validation_alias=AliasChoices(
            "observed_on", "observed_date", "date", "survey_date", "观测日"
        ),
    )
    districts: list[Identifier] = Field(
        min_length=2,
        max_length=2,
        description="互不相通的两个片区，顺序不影响求解",
        validation_alias=AliasChoices("districts", "areas", "zones", "片区"),
    )
    items: list[ErrataItem] = Field(
        min_length=1,
        max_length=16,
        description="当天两区内至多 16 个不同识别记录号；每条带 1～3 个备选耳标",
    )


class ErrataLineResult(BaseModel):
    recognition_id: str = Field(description="识别记录号")
    district: str = Field(description="片区（保持不变）")
    original_ear_tag: str = Field(description="勘误前耳标号")
    new_ear_tag: str = Field(description="勘误后耳标号（唯一最优时即为落库值）")
    changed: bool = Field(description="该记录的耳标是否被改写")


class ErrataPlanResult(BaseModel):
    changed_count: int = Field(description="本方案改写的记录数（优化目标，取最小值）")
    lines: list[ErrataLineResult] = Field(description="逐条新耳标，顺序与提交一致")


class ErrataRecaptureRow(BaseModel):
    district: str
    ear_tag: str
    day_count: int = Field(
        description="勘误落库后该耳标当天在该片区的识别记录条数；同区多拍保留"
    )


class EarTagErrataResponse(BaseModel):
    outcome: str = Field(
        description=(
            "applied：唯一最优且已落库；"
            "ambiguous：存在并列最优，返回两套方案且不写库；"
            "conflict：无解，不写库；"
            "stale：求解后数据已变，本次不写库，请重试"
        )
    )
    observed_on: date
    districts: list[str]
    solved_state_version: int | None = Field(
        default=None, description="求解所依据的已提交状态版本号"
    )
    committed_state_version: int | None = Field(
        default=None, description="落库后的已提交状态版本号（仅 applied）"
    )
    plans: list[ErrataPlanResult] = Field(
        default_factory=list,
        description="applied 时为唯一方案；ambiguous 时为两套并列最优方案",
    )
    recapture_summary: list[ErrataRecaptureRow] = Field(
        default_factory=list,
        description="落库后相关耳标当天在两区的复拍汇总（仅 applied）",
    )
    reasons: list[str] = Field(
        default_factory=list, description="conflict 或 stale 的具体原因"
    )
    retryable: bool = Field(
        default=False, description="stale 时为 true，提示按当前数据重新提交求解"
    )
