"""Read-only operational projections. No upstream call is made by these views."""
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from app.schemas import DTO


class ServiceStatus(DTO):
    id: UUID
    name: str
    kind: str
    status: str
    latest_data_at: str | None = None
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    error_code: str | None = None
    impact_modules: list[str] = Field(default_factory=list)


class TaskStatus(DTO):
    id: UUID
    kind: str
    feature: str
    service_name: str
    state: str
    stage: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None


class Issue(DTO):
    id: str
    title: str
    message: str
    connection_id: UUID | None = None
    job_id: UUID | None = None
    retry_at: datetime | None = None


class Overview(DTO):
    services_count: int
    running_tasks: int
    today_requests: int
    pending_issues: int
    services: list[ServiceStatus]
    tasks: list[TaskStatus]
    issues: list[Issue]
    updated_at: datetime
    statistics_timezone: Literal['Asia/Shanghai'] = 'Asia/Shanghai'


class CallRecord(DTO):
    id: UUID
    kind: Literal['ai', 'data']
    task: str
    service_name: str
    connection_id: UUID | None = None
    job_id: UUID | None = None
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    elapsed_ms: int | None = None
    request_count: int
    error_code: str | None = None
    retry_at: datetime | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_cny: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Calls(DTO):
    items: list[CallRecord]
    next_cursor: str | None = None
