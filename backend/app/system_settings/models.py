import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class UserPreference(Base):
    __tablename__ = 'user_preferences'
    __table_args__ = (
        CheckConstraint("start_page IN ('workbench','review','news')"),
        CheckConstraint('review_lookback IN (20,60)'),
        CheckConstraint("news_category IN ('精选','全部')"),
        CheckConstraint('revision >= 1'),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    start_page: Mapped[str] = mapped_column(String(20), default='workbench')
    review_lookback: Mapped[int] = mapped_column(default=20)
    news_category: Mapped[str] = mapped_column(String(20), default='精选')
    revision: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MaintenanceJob(Base):
    __tablename__ = 'maintenance_jobs'
    __table_args__ = (
        CheckConstraint("kind IN ('backup','update.download','update.install')"),
        CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled','manual_recovery')"),
        Index('ix_maintenance_claim', 'status', 'created_at', 'lease_until'),
        Index('uq_maintenance_active_exclusive', text('(1)'), unique=True,
              postgresql_where=text("kind IN ('backup','update.install') AND status IN ('queued','running')")),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default='queued')
    stage: Mapped[str] = mapped_column(String(80), default='queued')
    message: Mapped[str] = mapped_column(String(500), default='等待维护进程处理。')
    error_code: Mapped[str | None] = mapped_column(String(80))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[uuid.UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BackupRecord(Base):
    __tablename__ = 'backup_records'
    __table_args__ = (
        CheckConstraint("integrity_state IN ('pending','passed','failed')"),
        CheckConstraint("recovery_state IN ('unverified','passed','failed')"),
        CheckConstraint('size_bytes >= 0'),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'), index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('maintenance_jobs.id'), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    location_key: Mapped[str] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    integrity_state: Mapped[str] = mapped_column(String(20), default='pending')
    recovery_state: Mapped[str] = mapped_column(String(20), default='unverified')
    recovery_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    app_mode: Mapped[str] = mapped_column(String(10))
    schema_revision: Mapped[str] = mapped_column(String(80))
    build_revision: Mapped[str] = mapped_column(String(80))


class SystemState(Base):
    __tablename__ = 'system_settings_state'
    __table_args__ = (CheckConstraint('id=1'),)
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    active: Mapped[bool] = mapped_column(default=False)
    upgrade_job_id: Mapped[uuid.UUID | None]
    stage: Mapped[str] = mapped_column(String(80), default='idle')
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updater_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backup_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UpdateState(Base):
    __tablename__ = 'system_update_state'
    __table_args__ = (CheckConstraint('id=1'),)
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default='unchecked')
    message: Mapped[str] = mapped_column(String(500), default='尚未检查更新。')
    error_code: Mapped[str | None] = mapped_column(String(80))
    manifest: Mapped[dict | None] = mapped_column(JSONB)
    signature: Mapped[str | None] = mapped_column(Text)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    package_name: Mapped[str | None] = mapped_column(String(240))
    release_id: Mapped[str | None] = mapped_column(String(160))
