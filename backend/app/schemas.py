from datetime import date, datetime
from typing import Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class Quality(DTO):
    status: Literal["complete", "partial", "empty", "unavailable", "stale", "error"]
    reasons: list[str] = Field(default_factory=list)


class ReadCacheMeta(DTO):
    revision: str
    built_at: datetime
    validated_at: datetime
    state: Literal["fresh", "refreshing", "stale"]


class Meta(DTO):
    request_id: UUID
    app_mode: Literal["real", "demo"]
    generated_at: datetime
    as_of_time: datetime | None = None
    quality: Quality
    read_cache: ReadCacheMeta | None = None


T = TypeVar("T")


class Envelope(DTO, Generic[T]):
    data: T
    meta: Meta


class Page(DTO, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


class ErrorDetail(DTO):
    field: str
    reason: str


class ErrorBody(DTO):
    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)


class Error(DTO):
    error: ErrorBody
    request_id: UUID


class Health(DTO):
    status: Literal["ok", "unavailable"]
    request_id: UUID


class LoginInput(DTO):
    username: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=1024)


class User(DTO):
    id: UUID
    username: str
    role: Literal["viewer", "researcher", "admin"]


class Preferences(DTO):
    start_page: Literal['workbench', 'review', 'news'] = 'workbench'
    review_lookback: Literal[20, 60] = 20
    news_category: Literal['精选', '全部'] = '精选'
    revision: int = 1


class Session(DTO):
    user: User
    csrf_token: str
    cache_scope: str | None = None
    preferences: Preferences = Field(default_factory=Preferences)


class Rights(DTO):
    display: Literal["allowed", "denied", "unknown"]
    cache: Literal["allowed", "denied", "unknown"]
    export: Literal["allowed", "denied", "unknown"]
    redistribute: Literal["allowed", "denied", "unknown"]


class Provider(DTO):
    id: UUID
    name: str
    kind: Literal["manual", "market", "news", "calendar", "catalog", "model"]
    adapter_version: str
    configured: bool
    enabled: bool
    entitlement_status: Literal["verified", "denied", "unknown"]
    capabilities: list[str]
    rights: Rights


class Instrument(DTO):
    id: UUID
    exchange: Literal["SSE", "SZSE", "BSE"]
    symbol: str = Field(pattern=r"^[0-9]{6}$")
    name: str
    valid_from: date | None
    valid_to: date | None
    asset_type: Literal['stock', 'index', 'etf', 'convertible'] = 'stock'


class ResultRef(DTO):
    type: Literal["snapshot", "rule_run", "ai_interpretation"]
    id: UUID


class Job(DTO):
    id: UUID
    kind: Literal["snapshot.create", "rule.run", "ai.explain", "ai.generate", "news.ai", "system.check", "source.collect", "review.collect", "review.prepare"]
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    attempts: int
    result_ref: ResultRef | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
