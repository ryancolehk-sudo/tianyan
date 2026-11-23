import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PlanSettings(Base):
    __tablename__ = 'tomorrow_settings'
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'), primary_key=True)
    revision: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(default=False)
    use_ai: Mapped[bool] = mapped_column(default=True)
    profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanRun(Base):
    __tablename__ = 'tomorrow_runs'
    __table_args__ = (UniqueConstraint('owner_id', 'dedup_key'),
                     Index('ix_tomorrow_runs_owner_day', 'owner_id', 'source_trade_day', 'created_at'))
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('jobs.id'), unique=True)
    source_trade_day: Mapped[date]
    target_trade_day: Mapped[date | None]
    as_of_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    slot: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(30), default='queued')
    stage: Mapped[str] = mapped_column(String(120), default='等待研究资料准备')
    use_ai: Mapped[bool]
    dedup_key: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str | None] = mapped_column(String(64))
    bundle: Mapped[dict | None] = mapped_column(JSONB, deferred=True)
    stages: Mapped[list] = mapped_column(JSONB, default=list)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    limitations: Mapped[list] = mapped_column(JSONB, default=list)
    preparation: Mapped[dict] = mapped_column(JSONB, default=dict)
    base_report_id: Mapped[uuid.UUID | None]
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('tomorrow_snapshots.id'))
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlanSnapshot(Base):
    __tablename__ = 'tomorrow_snapshots'
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'))
    as_of_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    manifest: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    legacy_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('snapshots.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanReport(Base):
    __tablename__ = 'tomorrow_reports'
    __table_args__ = (UniqueConstraint('run_id', 'revision_number'),
                     UniqueConstraint('id', 'owner_id'),
                     Index('ix_tomorrow_reports_owner_time', 'owner_id', 'created_at'),
                     CheckConstraint('revision_number > 0'))
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('tomorrow_runs.id'))
    revision_number: Mapped[int]
    based_on_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('tomorrow_reports.id'))
    author_type: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSONB, deferred=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanConfirmation(Base):
    __tablename__ = 'tomorrow_confirmations'
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'))
    report_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('tomorrow_reports.id'), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanValidation(Base):
    __tablename__ = 'tomorrow_validations'
    __table_args__ = (Index('ix_tomorrow_validations_report_time', 'report_id', 'created_at'),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'))
    report_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('tomorrow_reports.id'))
    scenario_id: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(40))
    comment: Mapped[str] = mapped_column(String(4000))
    evidence_refs: Mapped[list] = mapped_column(JSONB)
    observation: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
