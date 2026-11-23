import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Connection(Base):
    __tablename__ = "service_connections"
    __table_args__ = (CheckConstraint("kind IN ('model','market','news')"), CheckConstraint("revision > 0"))
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    template: Mapped[str] = mapped_column(String(100), default="custom_model")
    endpoint: Mapped[str] = mapped_column(String(2000))
    protocol: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(default=False)
    revision: Mapped[int] = mapped_column(default=1)
    secret_ref: Mapped[str | None] = mapped_column(String(200))
    options: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_ids: Mapped[list] = mapped_column(JSONB, default=list)
    last_test: Mapped[dict | None] = mapped_column(JSONB)
    tested_revision: Mapped[int | None]
    archived: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiSettings(Base):
    __tablename__ = "ai_settings"
    __table_args__ = (CheckConstraint("id = 1"), CheckConstraint("revision > 0"))
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(default=False)
    default_connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("service_connections.id"))
    revision: Mapped[int] = mapped_column(default=1)


class AiBinding(Base):
    __tablename__ = "ai_bindings"
    feature: Mapped[str] = mapped_column(String(60), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("service_connections.id"))
    fallback_connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("service_connections.id"))
    revision: Mapped[int] = mapped_column(default=1)


class AiRun(Base):
    __tablename__ = "ai_runs"
    __table_args__ = (Index("ix_ai_runs_owner_created", "owner_id", "created_at", "id"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), unique=True)
    feature: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(30), default="queued")
    stage: Mapped[str] = mapped_column(String(120), default="等待 AI 执行器")
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("service_connections.id"))
    model: Mapped[str] = mapped_column(String(200))
    trade_day: Mapped[date | None]
    news_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evidence_versions.id"))
    as_of_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    config_snapshot: Mapped[dict] = mapped_column(JSONB)
    input_hash: Mapped[str] = mapped_column(String(64))
    dedup_key: Mapped[str] = mapped_column(String(64), unique=True)
    input_data: Mapped[dict] = mapped_column(JSONB)
    evidence_refs: Mapped[list] = mapped_column(JSONB, default=list)
    limitations: Mapped[list] = mapped_column(JSONB, default=list)
    result: Mapped[dict | None] = mapped_column(JSONB)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def billing_source(self):
        return "chatgpt_plan" if (self.config_snapshot or {}).get("billing_source") == "chatgpt_plan" else "api"


class AiAttempt(Base):
    __tablename__ = "ai_attempts"
    __table_args__ = (Index("ix_ai_attempts_time", "started_at", "id"), Index("ix_ai_attempts_run", "run_id"))
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ai_runs.id"))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    connection_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("service_connections.id"))
    purpose: Mapped[str] = mapped_column(String(20))
    service_name: Mapped[str] = mapped_column(String(200))
    model: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(30), default="preparing")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    elapsed_ms: Mapped[int | None]
    error_code: Mapped[str | None] = mapped_column(String(80))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_cny: Mapped[float | None] = mapped_column(Numeric(20, 8))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_count: Mapped[int] = mapped_column(default=0)
    safe_details: Mapped[dict] = mapped_column(JSONB, default=dict)


class AiProbeProof(Base):
    __tablename__ = "ai_probe_proofs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    result: Mapped[dict] = mapped_column(JSONB)
