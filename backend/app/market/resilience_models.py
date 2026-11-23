"""Durable recovery decisions; market evidence and source preferences stay immutable."""
import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DataHealth(Base):
    __tablename__ = 'data_health'
    __table_args__ = (
        UniqueConstraint('owner_id', 'primary_source_id', 'capability', 'scope_key', 'trade_day', name='uq_data_health_scope', postgresql_nulls_not_distinct=True),
        CheckConstraint("state IN ('normal','fallback','recovering','attention')"),
        Index('ix_data_health_owner_day', 'owner_id', 'trade_day'),
        Index('ix_data_health_action', 'state', 'next_action_at'),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID]
    primary_source_id: Mapped[uuid.UUID | None]
    active_source_id: Mapped[uuid.UUID | None]
    capability: Mapped[str] = mapped_column(String(100))
    scope_key: Mapped[str] = mapped_column(String(64))
    scope: Mapped[dict] = mapped_column(JSONB)
    trade_day: Mapped[date] = mapped_column(Date)
    state: Mapped[str] = mapped_column(String(20), default='recovering')
    stage: Mapped[str] = mapped_column(String(40), default='source')
    last_valid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_source_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(100))
    next_action: Mapped[str | None] = mapped_column(String(40))
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    missing_count: Mapped[int | None]
    failure_count: Mapped[int] = mapped_column(default=0)
    recovery_successes: Mapped[int] = mapped_column(default=0)
    last_probe_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[uuid.UUID | None]
    configuration_sha256: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
