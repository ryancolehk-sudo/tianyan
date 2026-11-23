import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ResearchDocument(Base):
    __tablename__ = "research_documents"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        ForeignKeyConstraint(["id", "head_revision_number"],
            ["document_revisions.document_id", "document_revisions.revision_number"],
            name="fk_document_head", deferrable=True, initially="DEFERRED", use_alter=True),
        CheckConstraint("kind IN ('review','direction','plan')"),
        CheckConstraint("head_revision_number > 0"),
        CheckConstraint("(kind='plan' AND target_trade_day IS NOT NULL AND target_trade_day > source_trade_day) OR (kind<>'plan' AND target_trade_day IS NULL)"),
        Index("ix_research_owner_created", "owner_id", "created_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(20))
    source_trade_day: Mapped[date]
    target_trade_day: Mapped[date | None]
    head_revision_number: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentRevision(Base):
    __tablename__ = "document_revisions"
    __table_args__ = (
        UniqueConstraint("document_id", "revision_number"),
        UniqueConstraint("id", "owner_id"),
        UniqueConstraint("id", "owner_id", "document_id"),
        ForeignKeyConstraint(["document_id", "owner_id"], ["research_documents.id", "research_documents.owner_id"]),
        ForeignKeyConstraint(["snapshot_id", "owner_id"], ["snapshots.id", "snapshots.owner_id"]),
        ForeignKeyConstraint(["based_on_revision_id", "owner_id", "document_id"],
            ["document_revisions.id", "document_revisions.owner_id", "document_revisions.document_id"]),
        CheckConstraint("revision_number > 0"),
        CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'"),
        Index("ix_revision_document_time", "document_id", "recorded_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID]
    owner_id: Mapped[uuid.UUID]
    revision_number: Mapped[int]
    based_on_revision_id: Mapped[uuid.UUID | None]
    title: Mapped[str] = mapped_column(String(200))
    snapshot_id: Mapped[uuid.UUID]
    calendar_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("calendar_versions.id"))
    content: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RevisionLink(Base):
    __tablename__ = "revision_links"
    __table_args__ = (
        ForeignKeyConstraint(["child_revision_id", "owner_id"], ["document_revisions.id", "document_revisions.owner_id"]),
        ForeignKeyConstraint(["parent_revision_id", "owner_id"], ["document_revisions.id", "document_revisions.owner_id"]),
        UniqueConstraint("child_revision_id", "position"),
        CheckConstraint("child_revision_id <> parent_revision_id"),
        CheckConstraint("relation IN ('review_direction','direction_plan')"),
    )
    child_revision_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    parent_revision_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[uuid.UUID]
    position: Mapped[int]
    relation: Mapped[str] = mapped_column(String(30))


class PublicationEvent(Base):
    __tablename__ = "publication_events"
    __table_args__ = (
        ForeignKeyConstraint(["revision_id", "owner_id"], ["document_revisions.id", "document_revisions.owner_id"]),
        UniqueConstraint("revision_id", "kind"),
        CheckConstraint("kind IN ('published','withdrawn')"),
        CheckConstraint("actor_id=owner_id"),
        CheckConstraint("kind<>'withdrawn' OR (reason IS NOT NULL AND length(trim(reason)) > 0)"),
        Index("ix_publication_revision_time", "revision_id", "recorded_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    revision_id: Mapped[uuid.UUID]
    owner_id: Mapped[uuid.UUID]
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(20))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(2000))


class PlanScenario(Base):
    __tablename__ = "plan_scenarios"
    __table_args__ = (
        ForeignKeyConstraint(["plan_revision_id", "owner_id"], ["document_revisions.id", "document_revisions.owner_id"]),
        UniqueConstraint("id", "plan_revision_id", "owner_id"),
        UniqueConstraint("plan_revision_id", "position"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    plan_revision_id: Mapped[uuid.UUID]
    owner_id: Mapped[uuid.UUID]
    position: Mapped[int]
    payload: Mapped[dict] = mapped_column(JSONB)
