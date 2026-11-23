import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ChatGPTInstallation(Base):
    __tablename__ = "chatgpt_installation"
    __table_args__ = (CheckConstraint("id = 1"),)
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    host_id: Mapped[str] = mapped_column(String(80), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatGPTIdentity(Base):
    __tablename__ = "chatgpt_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    issuer: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatGPTAccount(Base):
    __tablename__ = "chatgpt_accounts"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", "client_id"),
        CheckConstraint("revision > 0"),
        CheckConstraint("state IN ('connected','identity_only','disconnected','refreshing','refresh_unknown','reauthorization_required','client_error')"),
        Index("ix_chatgpt_accounts_owner", "owner_id", "created_at"),
        Index("uq_chatgpt_accounts_selected_owner", "owner_id", unique=True, postgresql_where=text("use_for_ai")),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("service_connections.id"), unique=True)
    issuer: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(512))
    client_id: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    label: Mapped[str] = mapped_column(String(200))
    secret_ref: Mapped[str | None] = mapped_column(String(200))
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    state: Mapped[str] = mapped_column(String(40), default="disconnected")
    use_for_ai: Mapped[bool] = mapped_column(default=False)
    revision: Mapped[int] = mapped_column(default=1)
    welcome_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_revocation_unconfirmed: Mapped[bool] = mapped_column(default=False)
    refresh_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatGPTAuthorization(Base):
    __tablename__ = "chatgpt_authorizations"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    nonce_hash: Mapped[str] = mapped_column(String(64))
    secret_ref: Mapped[str | None] = mapped_column(String(200))
    mode: Mapped[str] = mapped_column(String(10))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"))
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chatgpt_accounts.id"))
    client_id: Mapped[str | None] = mapped_column(String(200))
    redirect_uri: Mapped[str] = mapped_column(String(2000))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
