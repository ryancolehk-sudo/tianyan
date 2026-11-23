"""Disposable, owner-scoped projections; never a source of market evidence."""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ReadDependency(Base):
    __tablename__ = 'read_cache_dependencies'
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, default=0)
    last_txid: Mapped[int] = mapped_column(BigInteger, default=0)


class ReadCacheDispatch(Base):
    __tablename__ = 'read_cache_dispatch'
    __table_args__ = (CheckConstraint('id = 1'), CheckConstraint('turn BETWEEN 0 AND 3'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    turn: Mapped[int] = mapped_column(Integer, default=0, server_default='0')


class ReadCache(Base):
    __tablename__ = 'read_cache_entries'
    __table_args__ = (Index('ix_read_cache_queue', 'dirty', 'refresh_after'),
                      Index('ix_read_cache_retention', 'accessed_at'),
                      Index('ix_read_cache_dependency_keys', 'dependency_keys', postgresql_using='gin'))
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # No foreign key: deleting derived results must never interfere with source
    # retention, account removal, or restoration of a historical database.
    owner_id: Mapped[uuid.UUID] = mapped_column(index=True)
    scope: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(50))
    params: Mapped[dict] = mapped_column(JSONB)
    dependency_keys: Mapped[list] = mapped_column(JSONB)
    revision: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    focused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dirty: Mapped[bool] = mapped_column(default=True)
    lease_token: Mapped[uuid.UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(50))
