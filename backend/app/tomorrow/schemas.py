from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictBool, model_validator

from app.schemas import DTO

Slot = Literal['manual', 'evening', 'overnight']
ConditionStatus = Literal['pending', 'triggered', 'confirmed', 'invalidated', 'risk_veto', 'insufficient_data']


class Holding(DTO):
    code: str = Field(pattern=r'^(?:6\d{5}\.SH|[03]\d{5}\.SZ)$')
    quantity: int = Field(ge=0, le=1000000000)
    sellable_quantity: int = Field(ge=0, le=1000000000)
    cost: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    sector_codes: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def sellable(self):
        if self.sellable_quantity > self.quantity:
            raise ValueError('可卖数量不能超过持仓数量')
        return self


class RiskPolicy(DTO):
    approved: StrictBool = False
    max_total_weight: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    max_stock_weight: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    max_sector_weight: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    risk_per_trade: float | None = Field(default=None, gt=0, le=.1, allow_inf_nan=False)
    price_rule: Literal['none', 'prior_range_20'] = 'none'


class PlanSettingsPatch(DTO):
    expected_revision: int = Field(ge=0)
    model_connection_id: UUID | None = None
    enabled: StrictBool | None = None
    use_ai: StrictBool | None = None
    cash: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    holdings: list[Holding] | None = Field(default=None, max_length=200)
    risk_policy: RiskPolicy | None = None
    ai_source_ids: list[UUID] | None = Field(default=None, max_length=200)

    @model_validator(mode='after')
    def no_duplicates(self):
        if self.holdings and len({h.code for h in self.holdings}) != len(self.holdings):
            raise ValueError('持仓股票不能重复')
        return self


class PlanAiActivation(DTO):
    model_connection_id: UUID


class PlanReviewSelection(DTO):
    connection_id: UUID
    materials_digest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    enable_ai: StrictBool = False
    source_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=200)


class PlanCodexReview(PlanReviewSelection):
    model: str = Field(min_length=1, max_length=200, pattern=r'^[!-~]+$')


class PlanApiReview(PlanReviewSelection):
    connection_revision: int = Field(ge=1)
    enable_connection: StrictBool = False


class PlanRunInput(DTO):
    expected_settings_revision: int | None = Field(default=None, ge=0)
    model_connection_id: UUID | None = None
    codex_review: PlanCodexReview | None = None
    api_review: PlanApiReview | None = None
    source_trade_day: date | None = None
    target_trade_day: date | None = None
    as_of_time: AwareDatetime | None = None
    slot: Slot = 'manual'
    use_ai: StrictBool = False
    regenerate: StrictBool = False
    prepare: StrictBool = False

    @model_validator(mode='after')
    def explicit_ai_model(self):
        if self.codex_review or self.api_review:
            if (not self.use_ai or self.model_connection_id or self.expected_settings_revision is None
                    or (self.codex_review and self.api_review)):
                raise ValueError('本次模型复盘需要当前设置版本、AI 模式和唯一的模型选择。')
        if self.model_connection_id is not None and not self.use_ai:
            raise ValueError('指定模型时请选择 AI 复盘。')
        return self


class PlanRunView(DTO):
    id: UUID
    requested_model: str | None = None
    model_error: str | None = None
    source_trade_day: date
    target_trade_day: date | None
    slot: Slot
    state: str
    stage: str
    report_id: UUID | None = None
    as_of_time: AwareDatetime
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    error_code: str | None
    limitations: list[str]
    stages: list[dict]
    preparation: dict


class Judgment(DTO):
    object_type: Literal['market', 'sector', 'stock', 'event']
    object_id: str = Field(min_length=1, max_length=200)
    judgment: str = Field(min_length=1, max_length=3000)
    support_refs: list[str] = Field(default_factory=list, max_length=30)
    refuting_refs: list[str] = Field(default_factory=list, max_length=30)


class PlanRevisionInput(DTO):
    expected_revision: int = Field(ge=1)
    summary: str | None = Field(default=None, min_length=1, max_length=4000)
    judgments: list[Judgment] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


class ConfirmInput(DTO):
    expected_revision: int = Field(ge=1)


class PlanValidationInput(DTO):
    scenario_id: str = Field(min_length=1, max_length=200)
    status: ConditionStatus
    comment: str = Field(default='', max_length=4000)
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=100)


class PrepareInput(DTO):
    source_trade_day: date | None = None
    candidates: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode='after')
    def codes(self):
        import re
        if any(not re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)', s) for s in self.candidates):
            raise ValueError('候选代码格式错误')
        return self


class ReportSection(DTO):
    key: Literal['market', 'news', 'hotwords', 'sectors', 'stocks', 'limits', 'earnings', 'risks']
    title: str
    state: Literal['complete', 'partial', 'unavailable']
    summary: str
    paragraphs: list[str]
    analysis: list[dict]
    columns: list[dict[str, str]]
    rows: list[dict[str, str]]
    evidence_refs: list[str]
    limitations: list[str]


class PlanReportV1(DTO):
    can_export: bool = False
    format: Literal['tianyan.tomorrow.report.v1']
    source_trade_day: date
    target_trade_day: date | None
    as_of_time: AwareDatetime
    summary: str
    sections: list[ReportSection] = Field(min_length=8, max_length=8)
    dossier: dict
    judgments: list[dict]
    evidence_refs: list[dict]
    limitations: list[str]
    ai_completed_roles: list[str]
    ai_audit: dict | None = None
    audit_binding: dict | None = None
    revision_reason: str | None = None
    id: UUID
    run_id: UUID
    revision_number: int
    based_on_id: UUID | None
    slot: Slot
    status: Literal['draft', 'confirmed']
    author_type: Literal['rule', 'ai', 'manual']
    generation_models: list[str] = Field(default_factory=list)
    content_hash: str
    created_at: AwareDatetime
    confirmed_at: AwareDatetime | None


# Local convenience aliases preserve callers while keeping public OpenAPI
# names distinct from the already published AI/research/validation contracts.
SettingsPatch = PlanSettingsPatch
RunInput = PlanRunInput
RunView = PlanRunView
RevisionInput = PlanRevisionInput
ValidationInput = PlanValidationInput
