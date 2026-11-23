from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, SecretStr, StrictBool, field_validator, model_validator

from app.schemas import DTO

Protocol = Literal["openai_chat", "openai_responses", "anthropic_messages"]
Feature = Literal["theme_summary", "news_summary", "emotion_analysis", "direction_analysis", "plan_generation", "period_digest"]
FEATURES = {
    "theme_summary": ("工作台", "题材归纳", True),
    "emotion_analysis": ("每日复盘", "情绪研判", False),
    "direction_analysis": ("每日复盘", "方向与核心分析", False),
    "plan_generation": ("每日复盘", "明日预案生成", True),
    "news_summary": ("资讯中心", "新闻摘要", True),
    "period_digest": ("资讯中心", "日报 / 周报 / 月报", False),
}


class ConnectionOptions(DTO):
    timeout_seconds: int = Field(default=60, ge=5, le=180)
    max_output_tokens: int = Field(default=1200, ge=32, le=8192)
    input_price_per_million_cny: float | None = Field(default=None, ge=0, le=100000, allow_inf_nan=False)
    output_price_per_million_cny: float | None = Field(default=None, ge=0, le=100000, allow_inf_nan=False)


class ConnectionInput(DTO):
    id: UUID | None = None
    source_id: UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["model", "market", "news"]
    template: str = Field(default="custom_model", min_length=1, max_length=100)
    endpoint: str = Field(min_length=1, max_length=2000)
    protocol: Protocol | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None, repr=False)
    enabled: StrictBool = False
    source_ids: list[UUID] = Field(default_factory=list, max_length=100)
    options: ConnectionOptions = Field(default_factory=ConnectionOptions)
    expected_revision: int | None = Field(default=None, ge=1)
    test_proof: UUID | None = None

    @field_validator("name", "endpoint", "model")
    @classmethod
    def trim(cls, value):
        if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("invalid text")
        return value.strip() if value else value

    @field_validator("api_key")
    @classmethod
    def key_shape(cls, value):
        if value and value.get_secret_value():
            raw = value.get_secret_value()
            if not 8 <= len(raw) <= 2048 or any(ord(c) < 33 or ord(c) > 126 for c in raw):
                raise ValueError("invalid credential")
        return value

    @model_validator(mode="after")
    def model_protocol(self):
        if self.kind == "model" and self.protocol is None:
            raise ValueError("model protocol required")
        if self.kind != "model" and self.protocol is not None:
            raise ValueError("data connection cannot use a model protocol")
        return self


class ConnectionTest(DTO):
    status: Literal["success", "error", "unknown", "queued", "running"]
    at: AwareDatetime | None = None
    revision: int | None = None
    error_code: str | None = None
    elapsed_ms: int | None = None
    preview: str | None = None
    attempt_id: UUID | None = None
    job_id: UUID | None = None
    source_id: UUID | None = None
    request_count: int | None = None
    retry_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    capability: str | None = None
    message: str | None = None
    billing_source: Literal["api", "chatgpt_plan"] | None = None


class ConnectionView(DTO):
    id: UUID
    name: str
    kind: Literal["model", "market", "news"]
    template: str
    endpoint: str
    protocol: Protocol | None
    model: str | None
    owner_id: UUID | None = None
    enabled: bool
    revision: int
    credential_configured: bool
    credential_origin: Literal["vault", "environment", "none"]
    credential_error_code: str | None = None
    options: ConnectionOptions
    source_ids: list[UUID]
    capabilities: list[dict] = Field(default_factory=list)
    last_test: ConnectionTest | None
    tested_revision: int | None = None


class ConnectionTemplate(DTO):
    id: str
    name: str
    kind: Literal["model", "market", "news"]
    endpoint: str
    credential_required: bool
    endpoints: list[str] = Field(default_factory=list)
    source_families: list[str] = Field(default_factory=list)


class ConnectionIndex(DTO):
    items: list[ConnectionView]
    templates: list[ConnectionTemplate]
    credential_store_ready: bool


class ProbeResult(DTO):
    status: Literal["success", "error", "unknown", "queued", "running"]
    elapsed_ms: int | None = None
    error_code: str | None = None
    message: str
    preview: str | None = None
    models: list[dict] | None = None
    revision: int | None = None
    request_count: int = 0
    attempt_id: UUID | None = None
    test_proof: UUID | None = None
    job_id: UUID | None = None
    source_id: UUID | None = None
    capability: str | None = None


class FeatureBinding(DTO):
    id: Feature
    module: str
    name: str
    implemented: bool
    enabled: bool
    connection_id: UUID | None
    effective_connection_id: UUID | None
    fallback_connection_id: UUID | None


class SettingsView(DTO):
    enabled: bool
    default_connection_id: UUID | None
    revision: int
    personal_connection_id: UUID | None = None
    personal_model_ready: bool = False
    features: list[FeatureBinding]


class SettingsPatch(DTO):
    enabled: StrictBool | None = None
    default_connection_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class BindingPatch(DTO):
    enabled: StrictBool
    connection_id: UUID | None = None
    fallback_connection_id: UUID | None = None


class RunInput(DTO):
    feature: Feature
    trade_day: date | None = None
    as_of_time: AwareDatetime | None = None
    news_id: UUID | None = None
    regenerate: StrictBool = False


class GeneratedItem(DTO):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = Field(min_length=1, max_length=200)


class GeneratedResult(DTO):
    summary: str = Field(min_length=1, max_length=6000)
    items: list[GeneratedItem] = Field(min_length=1, max_length=20)


class ResearchCheck(DTO):
    window: Literal['09:25–09:59', '收盘后']
    subject: str = Field(min_length=1, max_length=200)
    support: str = Field(min_length=1, max_length=1000)
    invalidation: str = Field(min_length=1, max_length=1000)
    missing_data: str = Field(min_length=1, max_length=500)


class ResearchItem(GeneratedItem):
    object_type: Literal['market', 'sector', 'stock', 'event'] | None = None
    object_id: str | None = Field(default=None, max_length=200)
    section_key: Literal['market', 'news', 'hotwords', 'sectors', 'stocks', 'limits', 'earnings', 'risks'] | None = None
    direction_slot: Literal['primary', 'secondary', 'watch'] | None = None
    candidate_role: Literal['leader_candidate', 'capacity_candidate', 'relay_candidate', 'watch'] | None = None
    next_checks: list[ResearchCheck] = Field(default_factory=list, max_length=2)


class ResearchResult(GeneratedResult):
    items: list[ResearchItem] = Field(min_length=1, max_length=20)
    audit_status: Literal['passed', 'needs_review'] | None = None
    audit_issues: list[str] = Field(default_factory=list, max_length=20)


class RunView(DTO):
    id: UUID
    job_id: UUID
    feature: Feature
    state: str
    stage: str
    connection_id: UUID
    model: str
    billing_source: Literal["api", "chatgpt_plan"] = "api"
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    error_code: str | None
    input_hash: str
    evidence_refs: list[dict]
    limitations: list[str]
    result: GeneratedResult | ResearchResult | None
    usage: dict | None
    trade_day: date | None = None
    news_id: UUID | None = None
    as_of_time: AwareDatetime
