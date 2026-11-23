import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SourceVersion(Base):
    __tablename__ = "source_versions"
    __table_args__ = (Index("ix_source_version_cutoff", "source_id", "recorded_at", "id"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    descriptor: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))


class EvidenceVersion(Base):
    __tablename__ = "evidence_versions"
    __table_args__ = (
        UniqueConstraint("source_id", "owner_id", "source_key", "source_revision", name="uq_evidence_source_revision", postgresql_nulls_not_distinct=True),
        UniqueConstraint("id", "owner_id", "source_id", "source_key", name="uq_evidence_scope"),
        UniqueConstraint("source_id", "owner_id", "source_key", "version_number", name="uq_evidence_sequence", postgresql_nulls_not_distinct=True),
        ForeignKeyConstraint(["supersedes_id", "owner_id", "source_id", "source_key"],
                             ["evidence_versions.id", "evidence_versions.owner_id", "evidence_versions.source_id", "evidence_versions.source_key"]),
        CheckConstraint("(visibility='private' AND owner_id IS NOT NULL) OR (visibility='shared' AND owner_id IS NULL)"),
        CheckConstraint("kind IN ('manual','news','quote','bar','classification','calendar','reference')"),
        CheckConstraint("availability IN ('known','unknown')"),
        CheckConstraint("provenance IN ('manual','source_reported','unverified')"),
        CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'"),
        CheckConstraint("version_number > 0"),
        CheckConstraint("available_at IS NULL OR available_at >= received_at"),
        Index("ix_evidence_owner_cutoff", "owner_id", "source_id", "source_key", "available_at"),
        Index('ix_evidence_calendar_lookup', 'source_id', text("(payload->>'trade_day')"), 'received_at',
              postgresql_where=text("kind='calendar' AND owner_id IS NULL AND visibility='shared' AND provenance='source_reported'")),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id"))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    visibility: Mapped[str] = mapped_column(String(10), default="private")
    source_key: Mapped[str] = mapped_column(String(200))
    source_revision: Mapped[str] = mapped_column(String(200))
    version_number: Mapped[int]
    kind: Mapped[str] = mapped_column(String(30))
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("instruments.id"))
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    availability: Mapped[str] = mapped_column(String(10))
    provenance: Mapped[str] = mapped_column(String(20))
    historical_eligible: Mapped[bool]
    rights_basis: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)

    @property
    def authorship(self):
        return "original" if self.provenance == "manual" else "external"


class SourceHealth(Base):
    __tablename__ = "source_health"
    __table_args__ = (Index("ix_source_health_owner_time", "owner_id", "checked_at", "id"),
                      CheckConstraint("status IN ('ready','unknown','unavailable','error')"))
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id"))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    coverage: Mapped[dict] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(String(300))


class CalendarVersion(Base):
    __tablename__ = "calendar_versions"
    __table_args__ = (UniqueConstraint("exchange", "version"), CheckConstraint("coverage_end >= coverage_start"),
                      CheckConstraint("exchange IN ('SSE','SZSE','BSE')"), CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'"))
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    exchange: Mapped[str] = mapped_column(String(10))
    version: Mapped[str] = mapped_column(String(200))
    source_ref: Mapped[str] = mapped_column(String(2000))
    rights_basis: Mapped[str]
    coverage_start: Mapped[date]
    coverage_end: Mapped[date]
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    content_hash: Mapped[str] = mapped_column(String(64))


class TradingSession(Base):
    __tablename__ = "trading_sessions"
    calendar_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calendar_versions.id"), primary_key=True)
    trade_day: Mapped[date] = mapped_column(primary_key=True)
    is_open: Mapped[bool]
    segments: Mapped[list] = mapped_column(JSONB)


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        CheckConstraint("state IN ('pending','ready','failed')"),
        CheckConstraint("knowledge_mode='observed'"),
        CheckConstraint("as_of_time <= recorded_at"),
        CheckConstraint("(state='ready' AND manifest IS NOT NULL AND manifest_hash ~ '^[a-f0-9]{64}$') OR (state<>'ready' AND manifest IS NULL AND manifest_hash IS NULL)"),
        Index("ix_snapshots_owner_time", "owner_id", "recorded_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    as_of_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    knowledge_mode: Mapped[str] = mapped_column(String(20), default="observed")
    state: Mapped[str] = mapped_column(String(20), default="pending")
    selection: Mapped[dict] = mapped_column(JSONB)
    manifest: Mapped[dict | None] = mapped_column(JSONB)
    manifest_hash: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(80))


class SnapshotItem(Base):
    __tablename__ = "snapshot_items"
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("snapshots.id"), primary_key=True)
    evidence_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_versions.id"), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    method: Mapped[str] = mapped_column(String(10), primary_key=True)
    route: Mapped[str] = mapped_column(String(200), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[dict] = mapped_column(JSONB)
    status_code: Mapped[int]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
