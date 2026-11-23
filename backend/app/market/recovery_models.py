"""Rebuildable coverage ledger, separate from immutable market evidence."""
import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RecoveryCoverage(Base):
    __tablename__ = 'recovery_coverage'
    __table_args__ = (
        UniqueConstraint('owner_id', 'source_id', 'dataset', 'scope_key', 'trade_day', 'interval', name='uq_recovery_scope'),
        CheckConstraint("state IN ('missing','partial','complete','queued','running','waiting','blocked','unsupported')"),
        Index('ix_recovery_due', 'state', 'next_retry_at'),
        Index('ix_recovery_owner_day', 'owner_id', 'trade_day'),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # A derived ledger must not prevent deleting an old provider or account.
    # Authorization is rechecked against the live owner/provider on every use.
    owner_id: Mapped[uuid.UUID]
    source_id: Mapped[uuid.UUID]
    dataset: Mapped[str] = mapped_column(String(100))
    scope_key: Mapped[str] = mapped_column(String(64))
    scope: Mapped[dict] = mapped_column(JSONB)
    trade_day: Mapped[date] = mapped_column(Date)
    interval: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20), default='missing')
    expected_count: Mapped[int | None]
    actual_count: Mapped[int | None]
    missing_ranges: Mapped[list] = mapped_column(JSONB, default=list)
    job_id: Mapped[uuid.UUID | None]
    reason: Mapped[str | None] = mapped_column(String(100))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    configuration_sha256: Mapped[str] = mapped_column(String(64))
