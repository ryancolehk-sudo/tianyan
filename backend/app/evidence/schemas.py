from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, StrictBool, StringConstraints, field_validator, model_serializer, model_validator

from app.schemas import DTO, Provider, Quality

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
DecimalText = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,16}(\.[0-9]{1,8})?$", max_length=25)]
SharesText = Annotated[str, StringConstraints(pattern=r"^[0-9]+$", max_length=18)]
BarDecimalText = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,16}(\.[0-9]{1,10})?$", max_length=27)]
Exchange = Literal["SSE", "SZSE", "BSE"]
Kind = Literal["manual", "news", "quote", "bar", "classification", "calendar", "reference"]


def web_url(value):
    if value is not None:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("source link must be HTTP(S) and contain no credentials")
    return value


def safe_material(value):
    if isinstance(value, str):
        if "\0" in value:
            raise ValueError("material text cannot contain a null character")
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            safe_material(key); safe_material(item)
    elif isinstance(value, list):
        for item in value:
            safe_material(item)
    return value


class ManualPayload(DTO):
    text: str = Field(min_length=1, max_length=20000)
    source_url: str | None = Field(default=None, max_length=2000)
    _url = field_validator("source_url")(web_url)


class NewsSecurity(DTO):
    code: str | None = Field(default=None, pattern=r'^[0-9]{6}$')
    name: NonEmpty | None = None
    exchange: Exchange | None = None

    @model_validator(mode='after')
    def source_identity_present(self):
        if self.code is None and self.name is None:
            raise ValueError('a source security needs a code or a name')
        return self


class NewsPayload(DTO):
    title: NonEmpty
    summary: str | None = Field(min_length=1, max_length=20000)
    source_url: str = Field(max_length=2000)
    _url = field_validator("source_url")(web_url)
    category: Literal['政策', '公司', '行业', '公告', '海外', '财经'] | None = None
    source_name: NonEmpty | None = None
    published_day: date | None = None
    securities: list[NewsSecurity] = Field(default_factory=list, max_length=20)
    # Preserve publisher-shaped fields in older immutable payloads verbatim.
    secCode: str | None = Field(default=None, max_length=200)
    secName: str | None = Field(default=None, max_length=200)

    @model_serializer(mode='wrap')
    def retain_original_shape(self, handler):
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set}


class QuotePayload(DTO):
    price: DecimalText | None
    volume_shares: SharesText | None = None
    amount_cny: DecimalText | None = None
    quote_time: AwareDatetime | None
    source_snapshot_time: AwareDatetime | None = None
    price_unit: Literal['CNY', 'point'] = 'CNY'
    previous_close: DecimalText | None = None
    open: DecimalText | None = None
    high: DecimalText | None = None
    low: DecimalText | None = None
    instrument_name: str | None = Field(default=None, min_length=1, max_length=200)
    quote_kind: Literal['snapshot', 'daily_close', 'delayed_reference'] = 'snapshot'
    trade_day: date | None = None

    @model_validator(mode='after')
    def daily_close_has_date_only(self):
        if self.quote_kind in {'daily_close','delayed_reference'} and (self.trade_day is None or self.quote_time is not None):
            raise ValueError('a date-only quote needs a trade day, not an invented intraday timestamp')
        return self

    @model_serializer(mode='wrap')
    def retain_original_shape(self, handler):
        # Reading an old immutable payload must not insert new fields into its hash.
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set}


class BarPayload(DTO):
    arrival_kind: Literal['observed', 'backfill'] | None = None
    open: BarDecimalText
    high: BarDecimalText
    low: BarDecimalText
    close: BarDecimalText
    volume_shares: SharesText | None
    interval: Literal["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1M"]
    bar_end_at: AwareDatetime | None
    source_clock_at: AwareDatetime | None = None
    clock_semantics: Literal['start', 'end', 'unknown'] = 'unknown'
    volume_native: DecimalText | None = None
    amount_native: DecimalText | None = None
    trade_day: date | None = None
    price_unit: Literal['CNY', 'point'] = 'CNY'
    adjustment: Literal['unadjusted', 'forward', 'backward'] = 'unadjusted'
    amount_cny: DecimalText | None = None

    @model_validator(mode="after")
    def range_is_consistent(self):
        if not Decimal(self.low) <= min(Decimal(self.open), Decimal(self.close)) <= max(Decimal(self.open), Decimal(self.close)) <= Decimal(self.high):
            raise ValueError("bar prices must satisfy low <= open/close <= high")
        minute = self.interval in {'1m','5m','15m','30m','60m'}
        if minute and self.source_clock_at is not None:
            if self.trade_day != self.source_clock_at.astimezone(ZoneInfo('Asia/Shanghai')).date():
                raise ValueError('source minute clock and trade day must agree')
            if self.clock_semantics=='end' and self.bar_end_at!=self.source_clock_at:
                raise ValueError('end semantics must identify the same instant')
            if self.clock_semantics=='start' and self.bar_end_at is not None and self.bar_end_at<=self.source_clock_at:
                raise ValueError('known minute end must follow start')
        if minute and self.bar_end_at is None and self.source_clock_at is not None:
            pass
        elif self.bar_end_at is None and (self.trade_day is None or self.interval not in {'1d', '1w', '1M'}):
            raise ValueError('date-only bars need a declared daily/weekly/monthly period')
        if minute and self.clock_semantics == 'end' and self.bar_end_at is None:
            raise ValueError('known minute end requires an actual bar end clock')
        return self

    @model_serializer(mode='wrap')
    def retain_original_shape(self, handler):
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set}


class ReferenceColumn(DTO):
    key: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_]{0,59}$')
    label: NonEmpty
    unit: str | None = Field(default=None, max_length=100)


class ReferencePayload(DTO):
    """A bounded, versioned table; never coerce missing cells into market zeroes."""
    dataset: str = Field(pattern=r'^[a-z][a-z0-9_.]{1,79}$')
    title: NonEmpty
    source_url: str = Field(max_length=2000)
    attribution: NonEmpty
    scope: str = Field(min_length=1, max_length=2000)
    columns: list[ReferenceColumn] = Field(min_length=1, max_length=60)
    rows: list[dict[str, str | None]] = Field(max_length=8000)
    observed_day: date | None = None
    _url = field_validator('source_url')(web_url)

    @model_validator(mode='after')
    def consistent_table(self):
        keys = {column.key for column in self.columns}
        if len(keys) != len(self.columns) or any(set(row) != keys for row in self.rows):
            raise ValueError('reference rows must match the declared unique columns')
        if any(value is not None and (len(value) > 500 or '\0' in value) for row in self.rows for value in row.values()):
            raise ValueError('reference cell too long or invalid')
        return self


class ClassificationPayload(DTO):
    label: NonEmpty
    instrument_ids: list[UUID] = Field(max_length=5000)
    effective_from: AwareDatetime

    @field_validator("instrument_ids")
    @classmethod
    def no_duplicate_instruments(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("duplicate instrument reference")
        return values


class CalendarPayload(DTO):
    exchange: Exchange
    trade_day: date
    is_open: bool
    calendar_version: NonEmpty


Payload = ManualPayload | NewsPayload | QuotePayload | BarPayload | ClassificationPayload | CalendarPayload | ReferencePayload


class EvidenceInput(DTO):
    source_id: UUID
    source_key: NonEmpty
    source_revision: NonEmpty
    kind: Kind
    authorship: Literal["original", "external"] = "external"
    instrument_id: UUID | None = None
    event_at: AwareDatetime | None = None
    published_at: AwareDatetime | None = None
    payload: Payload
    rights_basis: str = Field(min_length=1, max_length=20000)
    supersedes_id: UUID | None = None
    _safe_text = model_validator(mode="before")(safe_material)

    @model_validator(mode="after")
    def payload_matches_kind(self):
        payload_type = {"manual": ManualPayload, "news": NewsPayload, "quote": QuotePayload,
                        "bar": BarPayload, "classification": ClassificationPayload, "calendar": CalendarPayload, "reference": ReferencePayload}
        if not isinstance(self.payload, payload_type[self.kind]):
            raise ValueError("evidence kind and payload do not match")
        if self.kind in {"quote", "bar"} and self.instrument_id is None:
            raise ValueError("a quote/bar requires an instrument")
        if type(self) is EvidenceInput and self.authorship == "original" and (
            self.kind != "manual" or self.payload.source_url is not None or self.published_at is not None
        ):
            raise ValueError("an original note cannot claim an external source or publication time")
        return self


class EvidenceVersion(EvidenceInput):
    id: UUID
    version_number: int
    owner_id: UUID | None
    visibility: Literal["private", "shared"]
    received_at: AwareDatetime
    available_at: AwareDatetime | None
    availability: Literal["known", "unknown"]
    provenance: Literal["manual", "source_reported", "unverified"]
    historical_eligible: bool
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    is_current: bool = False


class Coverage(DTO):
    expected: int | None
    accepted: int
    duplicates: int


class SourceHealth(DTO):
    id: UUID
    source_id: UUID
    checked_at: AwareDatetime
    status: Literal["ready", "unknown", "unavailable", "error"]
    last_event_at: AwareDatetime | None
    last_received_at: AwareDatetime | None
    coverage: Coverage
    reason: str


class Segment(DTO):
    start: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    end: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    kind: Literal["auction", "continuous"]

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("session segment end must follow start")
        return self


class SessionInput(DTO):
    trade_day: date
    is_open: StrictBool
    segments: list[Segment] = Field(max_length=12)

    @model_validator(mode="after")
    def valid_segments(self):
        if self.is_open != bool(self.segments):
            raise ValueError("open sessions require segments; closed sessions must have none")
        for first, second in zip(self.segments, self.segments[1:]):
            if second.start < first.end:
                raise ValueError("session segments overlap or are not sorted")
        return self


class CalendarInput(DTO):
    exchange: Exchange
    version: NonEmpty
    source_ref: str = Field(min_length=1, max_length=2000)
    rights_basis: str = Field(min_length=1, max_length=20000)
    coverage_start: date
    coverage_end: date
    sessions: list[SessionInput] = Field(min_length=1, max_length=3660)
    _safe_text = model_validator(mode="before")(safe_material)

    @model_validator(mode="after")
    def contiguous_coverage(self):
        days = (self.coverage_end - self.coverage_start).days + 1
        if not 1 <= days <= 3660 or len(self.sessions) != days:
            raise ValueError("every covered day must be explicitly declared")
        expected = {self.coverage_start + timedelta(days=i) for i in range(days)}
        if {s.trade_day for s in self.sessions} != expected:
            raise ValueError("calendar has duplicate, missing or out-of-range dates")
        self.sessions.sort(key=lambda s: s.trade_day)
        return self


class CalendarVersion(DTO):
    id: UUID
    exchange: Exchange
    version: str
    source_ref: str
    rights_basis: str
    coverage_start: date
    coverage_end: date
    recorded_at: AwareDatetime
    content_hash: str


class CalendarLookup(DTO):
    calendar_id: UUID | None
    exchange: Exchange
    requested_day: date
    calendar_version: str
    session: SessionInput | None
    reason: str | None


class ElapsedTime(DTO):
    seconds: int | None
    reason: str | None


class SnapshotInput(DTO):
    as_of_time: AwareDatetime
    required_capabilities: list[NonEmpty] = Field(max_length=40)
    source_keys: list[NonEmpty] | None = Field(default=None, min_length=1, max_length=5000)

    @field_validator("required_capabilities")
    @classmethod
    def unique_capabilities(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("duplicate required capability")
        return sorted(values)

    @field_validator("source_keys")
    @classmethod
    def selected_keys(cls, values):
        if values is not None and len(values) != len(set(values)):
            raise ValueError("duplicate source key")
        return sorted(values) if values is not None else None


class SnapshotItem(DTO):
    evidence_version_id: UUID
    content_hash: str


class SnapshotSource(DTO):
    version_id: UUID
    recorded_at: AwareDatetime
    content_hash: str
    descriptor: Provider


class Manifest(DTO):
    format: Literal["tianyan.snapshot.v1"]
    app_mode: Literal["real", "demo"]
    owner_id: UUID
    as_of_time: AwareDatetime
    knowledge_mode: Literal["observed"]
    required_capabilities: list[str]
    source_keys: list[str] | None
    sources: list[SnapshotSource]
    quality: Quality
    items: list[SnapshotItem]


class Snapshot(DTO):
    id: UUID
    owner_id: UUID
    state: Literal["pending", "ready", "failed"]
    as_of_time: AwareDatetime
    recorded_at: AwareDatetime
    knowledge_mode: Literal["observed"]
    manifest_hash: str | None
    quality: Quality
    items: list[SnapshotItem]
    manifest: Manifest | None
    error_code: str | None


class JobReceipt(DTO):
    job_id: UUID
    result_ref: "ResultRef"


from app.schemas import ResultRef  # noqa: E402
JobReceipt.model_rebuild()
