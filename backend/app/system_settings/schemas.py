from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas import DTO, Preferences


class PreferencesInput(DTO):
    start_page: Literal['workbench', 'review', 'news']
    review_lookback: Literal[20, 60]
    news_category: Literal['精选', '全部']
    expected_revision: int = Field(ge=1)


class PasswordInput(DTO):
    current_password: str = Field(min_length=1, max_length=1024, repr=False)
    new_password: str = Field(min_length=12, max_length=1024, repr=False)


class SessionItem(DTO):
    id: UUID
    created_at: datetime
    expires_at: datetime
    current: bool


class CacheStats(DTO):
    count: int
    bytes: int
    enabled: bool


class MaintenanceJob(DTO):
    id: UUID
    kind: Literal['backup', 'update.download', 'update.install']
    status: Literal['queued', 'running', 'succeeded', 'failed', 'cancelled', 'manual_recovery']
    stage: str
    message: str
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result: dict | None


class Backup(DTO):
    id: UUID
    name: str
    created_at: datetime
    completed_at: datetime | None
    size_bytes: int
    sha256: str | None
    integrity_state: Literal['pending', 'passed', 'failed']
    recovery_state: Literal['unverified', 'passed', 'failed']
    recovery_verified_at: datetime | None
    app_mode: Literal['real', 'demo']
    schema_revision: str
    build_revision: str


class MaintenanceInput(DTO):
    kind: Literal['backup']


class HealthItem(DTO):
    id: str
    label: str
    state: Literal['ok', 'warning', 'unavailable', 'disabled', 'unknown']
    checked_at: datetime
    message: str


class MaintenanceSummary(DTO):
    active: bool
    stage: str
    updater_connected: bool
    backup_connected: bool


class UpdateSummary(DTO):
    configured: bool
    state: str


class Overview(DTO):
    version: str
    app_mode: Literal['real', 'demo']
    health: list[HealthItem]
    cache: CacheStats
    last_backup: Backup | None
    maintenance: MaintenanceSummary
    update: UpdateSummary


class Activity(DTO):
    id: UUID
    at: datetime
    action: str
    label: str
    outcome: Literal['succeeded', 'failed', 'recorded']
    actor: str | None
    details: dict


class Diagnostics(DTO):
    version: str
    app_mode: Literal['real', 'demo']
    generated_at: datetime
    health: list[HealthItem]
    maintenance: MaintenanceSummary
    errors: list[str]
