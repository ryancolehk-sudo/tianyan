import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class NewsArticle(Base):
    __tablename__ = 'news_articles'
    __table_args__ = (UniqueConstraint('source_id', 'source_key'),
                     Index('ix_news_article_receipt', 'updated_at', 'id'),
                     Index('ix_news_article_duplicate', 'duplicate_key'))
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('providers.id'))
    source_key: Mapped[str] = mapped_column(String(200))
    latest_evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('evidence_versions.id'))
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    canonical_url: Mapped[str] = mapped_column(String(2000))
    duplicate_key: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(30))


class NewsUserState(Base):
    __tablename__ = 'news_user_states'
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id'), primary_key=True)
    article_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('news_articles.id'), primary_key=True)
    read_hash: Mapped[str | None] = mapped_column(String(64))
    read_versions: Mapped[list] = mapped_column(JSONB, default=list)
    saved: Mapped[bool] = mapped_column(default=False)
    following: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NewsCenterState(Base):
    __tablename__ = 'news_center_state'
    __table_args__ = (CheckConstraint('id = 1'),)
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_day: Mapped[date] = mapped_column(Date)
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NewsEdition(Base):
    __tablename__ = 'news_editions'
    __table_args__ = (UniqueConstraint('period', 'day'), UniqueConstraint('period', 'issue_number'),
                     CheckConstraint("period IN ('day','week','month')"),
                     CheckConstraint('window_end > window_start'),
                     Index('ix_news_edition_date', 'period', 'day'))
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    period: Mapped[str] = mapped_column(String(8))
    day: Mapped[date] = mapped_column(Date)
    issue_number: Mapped[int] = mapped_column(Integer)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recipe_version: Mapped[str] = mapped_column(String(80))
    coverage_status: Mapped[str] = mapped_column(String(12))
    coverage_note: Mapped[str] = mapped_column(String(2000))
    manifest: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))


class NewsEditionItem(Base):
    __tablename__ = 'news_edition_items'
    __table_args__ = (UniqueConstraint('edition_id', 'position'), UniqueConstraint('edition_id', 'article_id'))
    edition_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('news_editions.id'), primary_key=True)
    article_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('news_articles.id'), primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('evidence_versions.id'))
    position: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(30))
    late: Mapped[bool] = mapped_column(default=False)


class NewsSourceCoverage(Base):
    __tablename__ = 'news_source_coverage'
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('providers.id'), primary_key=True)
    fetch_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('data_fetch_runs.id'))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    coverage: Mapped[dict] = mapped_column(JSONB)


class NewsAiVersion(Base):
    __tablename__ = 'news_ai_versions'
    __table_args__ = (UniqueConstraint('edition_id'), CheckConstraint("state IN ('running','complete','failed')"))
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    edition_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('news_editions.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(12))
    connection_id: Mapped[str] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(80))
    input_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSONB)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))
