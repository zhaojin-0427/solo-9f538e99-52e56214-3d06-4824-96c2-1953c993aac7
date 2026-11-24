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
