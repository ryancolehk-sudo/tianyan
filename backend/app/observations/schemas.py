from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from app.evidence.schemas import NonEmpty, safe_material
from app.research.schemas import unique_references
from app.schemas import DTO

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]


class ObservationFact(DTO):
    field: NonEmpty
    value: Text
    unit: NonEmpty
    evidence_ref: UUID


class ObservationInput(DTO):
    plan_revision_id: UUID
    scenario_id: UUID
    snapshot_id: UUID
    observed_at: AwareDatetime
    status: Literal['observed', 'unavailable']
    facts: list[ObservationFact] = Field(max_length=100)
    commentary: Text | None
    evidence_refs: list[UUID] = Field(max_length=100)
    _safe = model_validator(mode='before')(safe_material)
    _refs = field_validator('evidence_refs')(unique_references)

    @model_validator(mode='after')
    def consistent_facts(self):
        if self.status == 'unavailable':
            if self.facts or self.evidence_refs or not self.commentary:
                raise ValueError('unavailable requires a missing-data reason and no facts/references')
        elif not self.facts or not self.evidence_refs or not {fact.evidence_ref for fact in self.facts} <= set(self.evidence_refs):
            raise ValueError('observed facts require explicit evidence references')
        if type(self) is ObservationInput and len(self.model_dump_json().encode()) > 500000:
            raise ValueError('observation exceeds 500 KB')
        return self


class Observation(ObservationInput):
    id: UUID
    owner_id: UUID
    recorded_at: AwareDatetime
    phase: Literal['09:15', '09:20', '09:25']
    timeliness: Literal['on_time', 'late']
    content_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
