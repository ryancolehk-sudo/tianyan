import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ScenarioValidation(Base):
    __tablename__ = 'validations'
    __table_args__ = (
        UniqueConstraint('id', 'owner_id', 'plan_revision_id', 'scenario_id'),
        UniqueConstraint('supersedes_id'),
        ForeignKeyConstraint(['scenario_id','plan_revision_id','owner_id'], ['plan_scenarios.id','plan_scenarios.plan_revision_id','plan_scenarios.owner_id']),
        ForeignKeyConstraint(['snapshot_id','owner_id'], ['snapshots.id','snapshots.owner_id']),
        ForeignKeyConstraint(['observation_id','owner_id','plan_revision_id','scenario_id'], ['auction_observations.id','auction_observations.owner_id','auction_observations.plan_revision_id','auction_observations.scenario_id']),
        ForeignKeyConstraint(['supersedes_id','owner_id','plan_revision_id','scenario_id'], ['validations.id','validations.owner_id','validations.plan_revision_id','validations.scenario_id']),
        CheckConstraint("outcome IN ('supported','refuted','inconclusive')"),
        CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'"),
        CheckConstraint('supersedes_id IS NULL OR supersedes_id<>id'),
        Index('ix_validation_plan_recorded', 'owner_id','plan_revision_id','recorded_at','id'),
        Index('ux_validation_root', 'owner_id','plan_revision_id','scenario_id', unique=True, postgresql_where=text('supersedes_id IS NULL')),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID]
    plan_revision_id: Mapped[uuid.UUID]
    scenario_id: Mapped[uuid.UUID]
    observation_id: Mapped[uuid.UUID | None]
    snapshot_id: Mapped[uuid.UUID]
    outcome: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str]
    evidence_refs: Mapped[list] = mapped_column(JSONB)
    supersedes_id: Mapped[uuid.UUID | None]
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
