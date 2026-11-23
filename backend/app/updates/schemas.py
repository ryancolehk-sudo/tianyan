from datetime import datetime
from typing import Literal
from pydantic import Field
from app.schemas import DTO
from app.system_settings.schemas import MaintenanceJob


class Release(DTO):
    id: str
    version: str
    published_at: datetime
    notes: str
    channel: Literal['stable']
    automatic_supported: bool
    reason: str | None
    downloaded: bool
    size_bytes: int


class Updates(DTO):
    configured: bool
    state: Literal['unconfigured', 'unchecked', 'checking', 'available', 'current', 'error', 'manual']
    message: str
    checked_at: datetime | None
    current_version: str
    release: Release | None
    download: MaintenanceJob | None
    install: MaintenanceJob | None


class DownloadInput(DTO):
    release_id: str = Field(min_length=1, max_length=100)


class InstallInput(DownloadInput):
    confirmed: Literal[True]
    drafts_saved: Literal[True]
