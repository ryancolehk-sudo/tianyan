from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.schemas import DTO
from app.evidence.schemas import NonEmpty, safe_material

Kind = Literal["review", "direction", "plan"]
PublicationStatus = Literal["draft", "published", "withdrawn"]
References = Annotated[list[UUID], Field(max_length=5000)]
Limitations = Annotated[list[NonEmpty], Field(max_length=40)]


def unique_references(values):
    if len(values) != len(set(values)):
        raise ValueError("duplicate reference")
    return sorted(values, key=str)


class ReviewContent(DTO):
    type: Literal["review"]
    summary: str = Field(min_length=1, max_length=20000)
    evidence_refs: References
    limitations: Limitations
    _refs = field_validator("evidence_refs")(unique_references)


class DirectionItem(DTO):
    label: NonEmpty
    core_instrument_ids: Annotated[list[UUID], Field(max_length=500)]
    judgment: str = Field(min_length=1, max_length=20000)
    evidence_refs: References
    _refs = field_validator("evidence_refs", "core_instrument_ids")(unique_references)


class DirectionContent(DTO):
    type: Literal["direction"]
    items: list[DirectionItem] = Field(min_length=1, max_length=40)
    limitations: Limitations


class ScenarioInput(DTO):
    label: NonEmpty
    instrument_ids: Annotated[list[UUID], Field(max_length=500)]
    trigger_text: str = Field(min_length=1, max_length=20000)
    invalidation_text: str = Field(min_length=1, max_length=20000)
    evidence_refs: References
    _refs = field_validator("evidence_refs", "instrument_ids")(unique_references)


class PlanContent(DTO):
    type: Literal["plan"]
    scenarios: list[ScenarioInput] = Field(min_length=1, max_length=40)
    limitations: Limitations


Content = Annotated[ReviewContent | DirectionContent | PlanContent, Field(discriminator="type")]


class RevisionInput(DTO):
    title: NonEmpty
    snapshot_id: UUID
    calendar_version_id: UUID | None = None
    upstream_revision_ids: Annotated[list[UUID], Field(max_length=100)]
    content: Content
    _safe = model_validator(mode="before")(safe_material)
    _refs = field_validator("upstream_revision_ids")(unique_references)

    @model_validator(mode="after")
    def bounded_document(self):
        if type(self) is RevisionInput and len(self.model_dump_json().encode()) > 500000:
            raise ValueError("research revision exceeds 500 KB")
        return self


class DocumentInput(DTO):
    kind: Kind
    source_trade_day: date
    target_trade_day: date | None = None
    revision: RevisionInput

    @model_validator(mode="after")
    def consistent_identity(self):
        if self.revision.content.type != self.kind:
            raise ValueError("document kind and content type differ")
        if self.kind == "plan":
            if self.target_trade_day is None or self.target_trade_day <= self.source_trade_day:
                raise ValueError("a plan needs a later target date")
        elif self.target_trade_day is not None:
            raise ValueError("only a plan has a target date")
        return self


class Document(DTO):
    id: UUID
    owner_id: UUID
    kind: Kind
    source_trade_day: date
    target_trade_day: date | None
    head_revision_id: UUID
    head_revision_number: int = Field(ge=1)
    head_revision_title: str
    head_publication_status: PublicationStatus
    created_at: AwareDatetime


class Scenario(ScenarioInput):
    id: UUID
    plan_revision_id: UUID


class Revision(RevisionInput):
    id: UUID
    document_id: UUID
    owner_id: UUID
    author_id: UUID
    revision_number: int = Field(ge=1)
    document_head_revision_number: int = Field(ge=1)
    based_on_revision_id: UUID | None
    publication_status: PublicationStatus
    published_at: AwareDatetime | None
    recorded_at: AwareDatetime
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    scenarios: list[Scenario]


class PublishInput(DTO):
    pass


class RevisionSummary(DTO):
    id: UUID
    document_id: UUID
    revision_number: int = Field(ge=1)
    based_on_revision_id: UUID | None
    title: str
    snapshot_id: UUID
    calendar_version_id: UUID | None
    publication_status: PublicationStatus
    published_at: AwareDatetime | None
    recorded_at: AwareDatetime
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
