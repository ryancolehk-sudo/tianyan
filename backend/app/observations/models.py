import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuctionObservation(Base):
    __tablename__ = 'auction_observations'
    __table_args__ = (
        UniqueConstraint('id','owner_id','plan_revision_id','scenario_id', name='uq_observation_validation_scope'),
        ForeignKeyConstraint(['scenario_id', 'plan_revision_id', 'owner_id'], ['plan_scenarios.id', 'plan_scenarios.plan_revision_id', 'plan_scenarios.owner_id']),
        ForeignKeyConstraint(['snapshot_id', 'owner_id'], ['snapshots.id', 'snapshots.owner_id']),
        CheckConstraint("status IN ('observed','unavailable')"),
        CheckConstraint("phase IN ('09:15','09:20','09:25')"),
        CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'"),
        CheckConstraint('observed_at <= recorded_at'),
        Index('ix_observation_plan_recorded', 'owner_id', 'plan_revision_id', 'recorded_at', 'id'),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID]
    plan_revision_id: Mapped[uuid.UUID]
    scenario_id: Mapped[uuid.UUID]
    snapshot_id: Mapped[uuid.UUID]
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    phase: Mapped[str] = mapped_column(String(5))
    status: Mapped[str] = mapped_column(String(20))
    facts: Mapped[list] = mapped_column(JSONB)
    commentary: Mapped[str | None]
    evidence_refs: Mapped[list] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
