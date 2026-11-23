import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ReviewArchive(Base):
    __tablename__ = "review_archives"
    __table_args__ = (
        UniqueConstraint("owner_id", "source_identity"),
        CheckConstraint("target_trade_day > based_on_trade_day"),
        CheckConstraint("source_version BETWEEN 1 AND 999999"),
        CheckConstraint("evidence_cutoff_at <= source_generated_at AND source_generated_at <= imported_at"),
        CheckConstraint("content_hash ~ '^[a-f0-9]{64}$' AND source_identity ~ '^[a-f0-9]{64}$'"),
        Index("ix_archive_owner_day_version", "owner_id", "target_trade_day", "source_version", "imported_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    source_identity: Mapped[str] = mapped_column(String(64))
    source_repository: Mapped[str] = mapped_column(String(100))
    source_commit: Mapped[str] = mapped_column(String(40))
    based_on_trade_day: Mapped[date]
    target_trade_day: Mapped[date]
    source_version: Mapped[int]
    source_stage: Mapped[str] = mapped_column(String(100))
    evidence_cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)
    summary_text: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
