from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.evidence.schemas import safe_material
from app.observations.schemas import Text
from app.research.schemas import unique_references
from app.schemas import DTO


class ValidationInput(DTO):
    plan_revision_id: UUID
    scenario_id: UUID
    observation_id: UUID | None
    snapshot_id: UUID
    outcome: Literal['supported', 'refuted', 'inconclusive']
    reason: Text
    evidence_refs: list[UUID] = Field(max_length=100)
    supersedes_id: UUID | None
    _safe = model_validator(mode='before')(safe_material)
    _refs = field_validator('evidence_refs')(unique_references)

    @model_validator(mode='after')
    def evidence_required(self):
        if self.outcome != 'inconclusive' and not self.evidence_refs:
            raise ValueError('supported/refuted requires evidence')
        return self


class Validation(ValidationInput):
    id: UUID
    owner_id: UUID
    recorded_at: AwareDatetime
    content_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    superseded_by_id: UUID | None
