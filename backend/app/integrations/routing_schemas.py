from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, StrictBool, field_validator

from app.schemas import DTO


class CapabilityOption(DTO):
    provider_id: str
    capability_id: str
    name: str
    billing: Literal['free', 'free_quota', 'points', 'subscription', 'unknown']
    billing_label: str
    billing_note: str
    reference_url: str
    checked_at: str | None
    entitlement: Literal['verified', 'locked', 'unknown']
    implemented: bool
    state: str
    last_success_at: str | None
    source_ids: list[str]
    selected: bool
    reason: str | None
    scope: str


class Supplier(DTO):
    id: str
    name: str
    template: str
    kind: str
    endpoint: str
    connection_id: str | None
    credential_required: bool
    credential_configured: bool
    enabled: bool
    serving_count: int
    capabilities: list[CapabilityOption]


class RouteCapability(DTO):
    id: str
    name: str
    group: str
    description: str
    routable: bool = True
    current_provider_id: str | None
    fallback_provider_ids: list[str] = Field(default_factory=list)
    effective_provider_id: str | None
    options: list[CapabilityOption]


class CapabilityMatrix(DTO):
    revision: int
    policy: Literal['purchased_first','free_first'] = 'purchased_first'
    providers: list[Supplier]
    capabilities: list[RouteCapability]


class RouteChange(DTO):
    capability_id: str = Field(min_length=1, max_length=80)
    provider_id: str = Field(min_length=1, max_length=100)
    fallback_provider_ids: list[str] = Field(default_factory=list, max_length=20)
    allow_points: StrictBool = False


class RoutesPatch(DTO):
    expected_revision: int = Field(ge=1)
    changes: list[RouteChange] = Field(min_length=1, max_length=40)


class ConfigurePorts(DTO):
    expected_revision: int = Field(ge=1)
    provider_id: str | None = Field(default=None, min_length=1, max_length=100)
    capability_id: str | None = Field(default=None, min_length=1, max_length=100)


class PortResult(DTO):
    provider_id: str
    capability_id: str
    state: Literal['configured', 'existing', 'attention']
    reason: str


class ConfiguredPorts(DTO):
    matrix: CapabilityMatrix
    items: list[PortResult]


class ProviderConnect(DTO):
    api_key: SecretStr | None = Field(default=None, repr=False)
    allow_points: StrictBool = False
    points_source_id: UUID | None = None

    @field_validator('api_key')
    @classmethod
    def key_shape(cls, value):
        if value and value.get_secret_value():
            raw = value.get_secret_value()
            if not 8 <= len(raw) <= 2048 or any(ord(c) < 33 or ord(c) > 126 for c in raw):
                raise ValueError('invalid credential')
        return value


class FullMinuteRequest(DTO):
    day: date | None = None
    cursor: str | None = Field(default=None, max_length=512)
    full_market: StrictBool = True
    allow_points: StrictBool = False


class MinuteChunkReceipt(DTO):
    job_id: str
    state: str
    request_limit: int
    scope_count: int


class MinuteJobView(DTO):
    job_id: str
    state: str
    error_code: str | None
    progress: dict | None
