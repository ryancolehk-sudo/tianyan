"""Operational findings and verifiable repair receipts."""
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas import DTO


class Finding(DTO):
    id: str
    title: str
    area: Literal['source', 'history', 'capability', 'runtime', 'cache', 'call', 'route']
    state: Literal['available', 'working', 'waiting', 'manual', 'limited', 'verified']
    advice: str
    count: int = 1
    source_id: UUID | None = None
    connection_id: UUID | None = None
    reason: str | None = None
    retry_at: datetime | None = None
    job_id: UUID | None = None
    trade_day: date | None = None


class RepairReceipt(Finding):
    configuration: str | None = None
    capability: str | None = None
    coverage_id: UUID | None = None
    coverage_signature: str | None = None
    backup_source_id: UUID | None = None


class RepairBatch(DTO):
    started_at: datetime
    actions: list[RepairReceipt] = Field(default_factory=list)


class Inspection(DTO):
    checked_at: datetime
    checks: dict[str, int]
    findings: list[Finding]
    last_repair: RepairBatch | None = None


class InspectionService(DTO):
    id: str
    name: str
    online: bool
    checked_at: datetime | None = None


class InspectionRun(DTO):
    id: UUID | None = None
    started_at: datetime
    phase: Literal['queued', 'scanning', 'applying', 'submitted', 'failed']
    checked: int = 0
    total: int | None = None
    current: str | None = None
    message: str | None = None
    batch: RepairBatch | None = None


class InspectionProgress(DTO):
    checked_at: datetime
    services: list[InspectionService]
    run: InspectionRun | None = None
