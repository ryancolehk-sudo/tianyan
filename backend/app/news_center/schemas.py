from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.schemas import DTO

Category = Literal['政策', '产业', '公司公告', '海外', '风险澄清', '财经']
Period = Literal['day', 'week', 'month']


class TopicEvent(DTO):
    id: UUID
    evidence_id: UUID
    title: str
    summary: str | None
    basis: Literal['source_explicit', 'manual_confirmed', 'keyword_clue']
    basis_note: str
    source_id: UUID
    source_name: str
    source_url: str | None
    published_at: AwareDatetime | None
    received_at: AwareDatetime
    content_hash: str


class TopicMember(DTO):
    code: str
    name: str
    pct_change: str | None
    evidence: dict[str, Any] | None


class TopicResearchView(DTO):
    topic_source: str
    topic_key: str
    keyword: str
    trade_day: date
    as_of_time: AwareDatetime
    events: list[TopicEvent]
    total: int
    next_cursor: str | None
    members: list[TopicMember]
    coverage: list[dict[str, Any]]
    gaps: list[str]


class RelatedSource(DTO):
    name: str
    url: str


class NewsSecurityView(DTO):
    code: str | None
    name: str | None
    exchange: Literal['SSE', 'SZSE', 'BSE'] | None
    basis: Literal['source_reported', 'archived_announcement']
    evidence_id: UUID
    source_id: UUID
    source_name: str
    source_url: str
    received_at: AwareDatetime
    content_hash: str
    source_fields: list[str]


class NewsArticleView(DTO):
    id: UUID
    evidence_id: UUID
    title: str
    summary: str | None
    summary_input_available: bool = False
    summary_input_reason: str | None = None
    category: Category
    source_name: str
    publisher: str
    family: str
    source_url: str
    published_at: AwareDatetime | None
    published_day: date | None
    received_at: AwareDatetime
    content_hash: str
    is_read: bool
    is_saved: bool
    is_following: bool
    related_sources: list[RelatedSource] = Field(default_factory=list)
    late: bool = False
    securities: list[NewsSecurityView] = Field(default_factory=list)
    security_status: Literal['source_provided', 'missing', 'unverified'] = 'missing'


class NewsSourceView(DTO):
    family: str
    name: str
    publisher: str
    status: str
    last_success_at: AwareDatetime | None
    latest_published_at: AwareDatetime | None
    cached_count: int
    coverage: str | None


class NewsAiStatus(DTO):
    state: Literal['unconfigured', 'disabled', 'ready', 'error']
    connection_id: str | None
    model: str | None
    reason: str


class EditionSummary(DTO):
    id: UUID
    period: Period
    day: date
    issue_number: int
    headline: str
    item_count: int
    source_count: int
    reading_minutes: int
    window_start: AwareDatetime
    window_end: AwareDatetime
    generated_at: AwareDatetime
    coverage_status: Literal['complete', 'partial', 'empty']
    coverage_note: str
    ai_state: str


class NewsFeedView(DTO):
    items: list[NewsArticleView]
    next_cursor: str | None
    total: int
    focus: NewsArticleView | None
    latest_edition: EditionSummary | None
    sources: list[NewsSourceView]
    coverage_start: AwareDatetime | None
    ai_status: NewsAiStatus


class EditionList(DTO):
    items: list[EditionSummary]
    next_cursor: str | None


class AiEditionItem(DTO):
    article_id: UUID
    evidence_id: UUID
    summary: str | None
    category: Category
    selected: bool


class AiEditionView(DTO):
    id: UUID
    generated_at: AwareDatetime
    model: str
    prompt_version: str
    lead: str
    items: list[AiEditionItem]
    usage: dict | None


class EditionDetail(DTO):
    edition: EditionSummary
    items: list[NewsArticleView]
    ai: AiEditionView | None


class NewsStateUpdate(DTO):
    read: bool | None = None
    saved: bool | None = None
    following: bool | None = None
    evidence_id: UUID | None = None

    @model_validator(mode='after')
    def nonempty(self):
        changes = self.model_fields_set - {'evidence_id'}
        if not changes or any(getattr(self, key) is None for key in changes):
            raise ValueError('provide at least one boolean reading state')
        if 'evidence_id' in self.model_fields_set and (self.evidence_id is None or 'read' not in changes):
            raise ValueError('evidence_id applies only to a read state')
        return self
