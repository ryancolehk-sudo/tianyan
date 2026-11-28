"""Inputs from the feature catalog and receipts for one coverage repair."""
from uuid import UUID

from pydantic import Field

from app.integrations.inspection_schemas import RepairBatch
from app.schemas import DTO


class CoverageTarget(DTO):
    need_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]*$', max_length=64)
    port_ids: list[str] = Field(default_factory=list, max_length=12)
    source_ids: list[UUID] = Field(default_factory=list, max_length=96)
    gap: bool


class CoverageRepairInput(DTO):
    expected_revision: int = Field(ge=1)
    targets: list[CoverageTarget] = Field(min_length=1, max_length=100)


class CoverageRepairReport(RepairBatch):
    # Shared source/history actions are executed once and referenced by each affected need.
    by_need: dict[str, list[str]]


class CoverageRepairStatus(DTO):
    last_repair: CoverageRepairReport | None = None
