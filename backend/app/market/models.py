import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SourceSettings(Base):
    __tablename__ = 'source_settings'
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('providers.id'), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'))
    rights_basis: Mapped[str]
    config: Mapped[dict] = mapped_column(JSONB)
    interval_seconds: Mapped[int]
    configured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    paused_reason: Mapped[str | None] = mapped_column(String(80))
    legacy_cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), deferred=True)


class Watchlist(Base):
    __tablename__ = 'watchlists'
    __table_args__ = (CheckConstraint("jsonb_typeof(instrument_ids) = 'array' AND jsonb_array_length(instrument_ids) <= 100"),)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'), primary_key=True)
    instrument_ids: Mapped[list[str]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InstrumentOrigin(Base):
    """Which source currently claims a stable instrument identity.

    The displayed name is a shared attribute; this row records the source claim
    that must be rechecked against current display/cache rights at read time.
    Closing collection does not erase the claim; revoking display hides it.
    """
    __tablename__ = 'instrument_origins'
    __table_args__ = (
        UniqueConstraint('instrument_id', 'source_id', name='uq_instrument_origin_source'),
        CheckConstraint("origin_kind IN ('catalog','quote')"),
        CheckConstraint("last_content_hash ~ '^[a-f0-9]{64}$'"),
        CheckConstraint('last_observed_at >= first_observed_at'),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    instrument_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('instruments.id'), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('providers.id'))
    origin_kind: Mapped[str] = mapped_column(String(20))
    observed_name: Mapped[str] = mapped_column(String(200))
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_content_hash: Mapped[str] = mapped_column(String(64))
    source_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('source_versions.id'))


class DataFetchRun(Base):
    __tablename__ = 'data_fetch_runs'
    __table_args__ = (Index('ix_fetch_source_capability_time', 'source_id', 'capability', 'received_at', 'id'),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('providers.id'))
    capability: Mapped[str] = mapped_column(String(80))
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('jobs.id'))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(80))
    expected: Mapped[int | None]
    accepted: Mapped[int | None]
    rejected: Mapped[int | None]
    duplicates: Mapped[int | None]
    elapsed_ms: Mapped[int | None]
    request_count: Mapped[int]
    cost_cny: Mapped[Decimal | None] = mapped_column(Numeric())
    body_hash: Mapped[str | None] = mapped_column(String(64))
    # Deferred so older-schema upgrade fixtures can still read legacy columns.
    retry_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), deferred=True)

    @property
    def clock_regressed(self):
        return self.started_at is not None and self.received_at is not None and self.received_at<self.started_at
