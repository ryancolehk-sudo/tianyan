import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ScreenRule(Base):
    __tablename__ = "screen_rules"
    __table_args__ = (CheckConstraint("version >= 1"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    version: Mapped[int]
    content: Mapped[dict] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(default=False)
    transition_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScreenRuleVersion(Base):
    __tablename__ = "screen_rule_versions"
    __table_args__ = (UniqueConstraint("rule_id", "version", name="uq_screen_rule_version"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("screen_rules.id"))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int]
    content: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScreenRun(Base):
    __tablename__ = "screen_runs"
    __table_args__ = (
        UniqueConstraint(
            "rule_id", "rule_version", "input_hash", "mode", name="uq_screen_run_input"
        ),
        Index("ix_screen_runs_owner_time", "owner_id", "created_at"),
        CheckConstraint("mode IN ('review','observed','monitor')"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("screen_rules.id"))
    rule_version: Mapped[int]
    rule_name: Mapped[str] = mapped_column(String(80))
    content: Mapped[dict] = mapped_column(JSONB)
    trade_day: Mapped[date]
    as_of_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mode: Mapped[str] = mapped_column(String(20))
    engine_version: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64))
    data_revision: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[list] = mapped_column(JSONB)
    source_refs: Mapped[list] = mapped_column(JSONB)
    result: Mapped[dict] = mapped_column(JSONB)
    reasons: Mapped[list] = mapped_column(JSONB)
    event_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScreenEvent(Base):
    __tablename__ = "screen_events"
    __table_args__ = (
        UniqueConstraint("run_id", "code", "direction", name="uq_screen_event_transition"),
        CheckConstraint("direction IN ('entered','exited')"),
        Index("ix_screen_events_owner_time", "owner_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("screen_rules.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("screen_runs.id"))
    rule_name: Mapped[str] = mapped_column(String(80))
    rule_version: Mapped[int]
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
    direction: Mapped[str] = mapped_column(String(20))
    source_time: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
