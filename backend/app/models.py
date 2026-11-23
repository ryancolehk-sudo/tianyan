import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class InstanceState(Base):
    __tablename__ = "instance_state"
    __table_args__ = (CheckConstraint("id = 1"), CheckConstraint("app_mode IN ('real','demo')"))
    id: Mapped[int] = mapped_column(primary_key=True)
    app_mode: Mapped[str] = mapped_column(String(10))
    # Deferred so pre-0016 migration fixtures can still inspect the old instance.
    cache_id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, deferred=True)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('admin','researcher','viewer')"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoginSession(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    bucket: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(default=1)
    resets_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index('ix_audit_action_time', 'action', 'created_at'),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(80))
    request_id: Mapped[uuid.UUID | None]
    resource_type: Mapped[str | None] = mapped_column(String(80))
    resource_id: Mapped[uuid.UUID | None]
    changed_field_names: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    # Legacy upgrade fixtures and pre-P3 descriptors do not need this new column.
    # Only the P3 market domain loads it after migration 0007 has been applied.
    family: Mapped[str | None] = mapped_column(String(80), unique=True, deferred=True)
    adapter_version: Mapped[str] = mapped_column(String(80), default="not-installed")
    configured: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=False)
    entitlement_status: Mapped[str] = mapped_column(String(20), default="unknown")
    capabilities: Mapped[list] = mapped_column(JSONB, default=list)
    rights: Mapped[dict] = mapped_column(JSONB)


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("exchange", "asset_type", "symbol", "valid_from", postgresql_nulls_not_distinct=True),
        CheckConstraint("exchange IN ('SSE','SZSE','BSE')"),
        CheckConstraint("symbol ~ '^[0-9]{6}$'"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    exchange: Mapped[str] = mapped_column(String(10))
    symbol: Mapped[str] = mapped_column(String(6), index=True)
    name: Mapped[str] = mapped_column(String(200))
    asset_type: Mapped[str] = mapped_column(String(20), default="stock", server_default="stock")
    first_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Deferred so historical migration fixtures can still load pre-0009 rows.
    local_name: Mapped[str | None] = mapped_column(String(200), deferred=True)
    valid_from: Mapped[date | None]
    valid_to: Mapped[date | None]


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("state IN ('queued','running','succeeded','failed','cancelled')"),
        CheckConstraint("attempts >= 0 AND attempts <= 5"),
        Index("ix_jobs_claim", "state", "available_at", "lease_until"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    result_ref: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))
    lease_token: Mapped[uuid.UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), deferred=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(deferred=True)


# Register phase-specific tables for Alembic without giving them ownership of P1 data.
from app.evidence.models import CalendarVersion, EvidenceVersion, IdempotencyKey, Snapshot, SnapshotItem, SourceHealth, TradingSession  # noqa: E402,F401
from app.market.models import DataFetchRun, InstrumentOrigin, SourceSettings  # noqa: E402,F401
from app.ai.models import AiAttempt, AiBinding, AiProbeProof, AiRun, AiSettings, Connection  # noqa: E402,F401
from app.screening.models import ScreenRule, ScreenRuleVersion, ScreenRun, ScreenEvent  # noqa: E402,F401
from app.integrations.routing_models import DataRoutes  # noqa: E402,F401
from app.market.recovery_models import RecoveryCoverage  # noqa: E402,F401
