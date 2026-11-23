from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.schemas import DTO

Metric = Literal[
    "price",
    "pct_change",
    "speed_5m",
    "amount_cny",
    "turnover_rate",
    "float_mv_cny",
    "open_pct",
    "previous_height",
    "volume_ratio",
    "auction_pct", "auction_amount_cny", "auction_turnover_rate", "auction_volume_ratio",
    "yesterday_up", "yesterday_broken", "yesterday_lhb",
]
ResultState = Literal["matched", "not_matched", "insufficient"]


class Condition(DTO):
    field: Metric
    operator: Literal["gt", "gte", "lt", "lte", "eq"]
    value: str = Field(min_length=1, max_length=40, pattern=r"^-?\d{1,18}(?:\.\d{1,12})?$")

    @field_validator("value")
    @classmethod
    def finite_value(cls, value):
        try:
            number = Decimal(value)
            if not number.is_finite():
                raise ValueError()
        except InvalidOperation, ValueError:
            raise ValueError("条件必须为有限十进制数。") from None
        return value


class Definition(DTO):
    conditions: list[Condition] = Field(min_length=1, max_length=12)
    sort_field: Metric = "amount_cny"
    sort_direction: Literal["asc", "desc"] = "desc"


class RuleSave(DTO):
    id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=80)
    scope: Literal["market", "watchlist"] = "market"
    definition: Definition
    enabled: bool = False
    cooldown_seconds: int = Field(default=300, ge=60, le=86400)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("请输入观察规则名称。")
        return value

    @model_validator(mode="after")
    def version_required(self):
        from app.screening.engine import METRICS
        if any(c.field not in METRICS for c in self.definition.conditions) or self.definition.sort_field not in METRICS:
            raise ValueError('竞价条件只支持竞价成交筛选，不启用盘中自动观察。')
        if (self.id is None) != (self.expected_version is None):
            raise ValueError("修改规则必须携带当前版本。")
        return self


class Rule(DTO):
    id: UUID
    version: int
    name: str
    scope: Literal["market", "watchlist"]
    definition: Definition
    enabled: bool
    cooldown_seconds: int
    last_run_at: AwareDatetime | None
    last_error: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class RuleList(DTO):
    items: list[Rule]
    limit: int = 20


class ContextInput(DTO):
    day: date | None = None
    as_of_time: AwareDatetime | None = None


class PreviewInput(ContextInput):
    definition: Definition
    scope: Literal["market", "watchlist"] = "market"
    state: ResultState | None = None
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=100, ge=1, le=200)
    screen_context: Literal['market', 'auction'] = 'market'


class Check(DTO):
    field: Metric
    operator: Literal["gt", "gte", "lt", "lte", "eq"]
    target: str
    actual: str | None
    state: ResultState
    reason: str


class ResultItem(DTO):
    code: str
    name: str
    instrument_id: str | None = None
    source_time: str | None = None
    received_at: str | None = None
    metrics: dict[str, str | None]
    state: ResultState
    checks: list[Check]


class Counts(DTO):
    total: int
    matched: int
    not_matched: int
    insufficient: int


class SourceRef(DTO):
    evidence_id: UUID
    source_id: UUID
    content_hash: str
    received_at: AwareDatetime


class RunSummary(DTO):
    id: UUID
    rule_id: UUID
    rule_version: int
    rule_name: str
    trade_day: date
    as_of_time: AwareDatetime
    mode: Literal["review", "observed", "monitor"]
    counts: Counts
    created_at: AwareDatetime
    event_count: int
    input_hash: str
    engine_version: str


class RunResult(DTO):
    id: UUID | None = None
    rule_id: UUID | None = None
    rule_version: int | None = None
    rule_name: str | None = None
    definition: Definition
    scope: Literal["market", "watchlist"]
    trade_day: date
    as_of_time: AwareDatetime
    mode: Literal["review", "observed", "monitor"]
    counts: Counts
    items: list[ResultItem]
    offset: int = 0
    limit: int = 100
    has_more: bool = False
    event_count: int = 0
    input_hash: str
    data_revision: str
    engine_version: str
    source_refs: list[SourceRef]
    created_at: AwareDatetime
    reasons: list[str]
    screen_context: Literal['market', 'auction'] = 'market'
    coverage: dict = Field(default_factory=dict)


class RunList(DTO):
    items: list[RunSummary]


class Event(DTO):
    id: UUID
    rule_id: UUID
    rule_name: str
    rule_version: int
    run_id: UUID
    code: str
    name: str
    direction: Literal["entered", "exited"]
    source_time: str | None
    created_at: AwareDatetime


class EventList(DTO):
    items: list[Event]


class MetricInfo(DTO):
    field: Metric
    label: str
    unit: str
    description: str


class Catalogue(DTO):
    engine_version: str
    metrics: list[MetricInfo]
    interval_seconds: int = 60
    scope: str
    boundaries: list[str]
