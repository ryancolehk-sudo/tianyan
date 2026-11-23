"""Private, versioned daily-review journals; separate from market/P2 documents.

Only the existing manual provider is used. The business date never becomes a
publication/receipt timestamp. Every save appends original manual evidence and
compares the current head under the same transaction lock as evidence imports.
"""

import json
import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StringConstraints, ValidationError, model_validator
from sqlalchemy import and_, or_, select

from app.auth import APIError, require_role
from app.evidence.common import lock, now
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import EvidenceInput, ManualPayload, safe_material
from app.evidence.service import import_evidence
from app.models import Provider
from app.schemas import DTO

MANUAL_SOURCE_ID = uuid.uuid5(uuid.NAMESPACE_URL, "jiulong-pro-max/provider/manual")
FORMAT = "tianyan.review-notes.v1"
Stage = Literal["未标记", "退潮", "冰点", "修复", "启动", "发酵", "分歧", "局部修复", "轮动", "低迷"]
Slot = Literal["主线候选", "次主线", "新方向", "轮动"]
Tag = Literal["持续跟踪", "待验证", "观察", "新增", "风险提示"]
Section = Literal["summary", "directions", "lhb"]
SectorCode = Annotated[str, StringConstraints(pattern=r"^BK\d{4,8}\.DC$")]
StockCode = Annotated[str, StringConstraints(pattern=r"^(?:6\d{5}\.SH|[03]\d{5}\.SZ)$")]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class ReviewDirection(DTO):
    slot: Slot
    sector_code: SectorCode
    name: Name
    rationale: str = Field(default="", max_length=1500)


class ReviewThemeStage(DTO):
    sector_code: SectorCode
    stage: Stage = "未标记"


class ReviewCoreStock(DTO):
    code: StockCode
    name: Name
    sector_code: SectorCode | None = None
    role: str = Field(default="", max_length=200)
    rationale: str = Field(default="", max_length=1500)
    tag: Tag = "观察"


class ReviewObservation(DTO):
    text: str = Field(min_length=1, max_length=1000)
    tag: str = Field(default="观察", min_length=1, max_length=30)


class ReviewLhbNote(DTO):
    record_key: str = Field(min_length=1, max_length=200)
    stock_code: StockCode
    text: str = Field(default="", max_length=2000)
    confirmed: StrictBool = False
    evidence_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class ReviewCatalystLink(DTO):
    evidence_id: uuid.UUID
    source_row: int = Field(ge=0, strict=True)
    sector_code: SectorCode
    comment: str = Field(default="", max_length=1000)


class ReviewNotesContent(DTO):
    format: Literal["tianyan.review-notes.v1"] = FORMAT
    stage: Stage = "未标记"
    summary: str = Field(default="", max_length=4000)
    money_effect: str = Field(default="", max_length=1500)
    judgment: str = Field(default="", max_length=4000)
    support_evidence: str = Field(default="", max_length=2000)
    refuting_evidence: str = Field(default="", max_length=2000)
    data_evidence_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    directions: list[ReviewDirection] = Field(default_factory=list, max_length=4)
    theme_stages: list[ReviewThemeStage] = Field(default_factory=list, max_length=20)
    core_stocks: list[ReviewCoreStock] = Field(default_factory=list, max_length=30)
    observations: list[ReviewObservation] = Field(default_factory=list, max_length=20)
    lhb_notes: list[ReviewLhbNote] = Field(default_factory=list, max_length=20)
    catalyst_links: list[ReviewCatalystLink] = Field(default_factory=list, max_length=20)
    confirmed_sections: list[Section] = Field(default_factory=list, max_length=3)
    _safe_text = model_validator(mode="before")(safe_material)

    @model_validator(mode="after")
    def consistent_content(self):
        identities = [
            [item.slot for item in self.directions],
            [item.sector_code for item in self.theme_stages],
            [item.code for item in self.core_stocks],
            [item.record_key for item in self.lhb_notes],
            self.confirmed_sections,
        ]
        if any(len(set(keys)) != len(keys) for keys in identities):
            raise ValueError("review note slots, stock/theme keys and confirmations must be unique")
        if len(encode_content(self)) > 20000:
            raise ValueError("review note JSON must not exceed 20000 characters")
        return self


class ReviewNotesInput(DTO):
    trade_day: date
    expected_head: uuid.UUID | None
    content: ReviewNotesContent


def encode_content(content):
    """The ManualPayload character limit applies to the complete JSON wrapper."""
    return json.dumps(content.model_dump(mode="json"), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"), allow_nan=False)


def notes_key(trade_day):
    return "review.notes:" + _day(trade_day).isoformat()


def _day(value):
    if isinstance(value, datetime):
        raise APIError(422, "review_notes_date_invalid", "复盘笔记日期格式不正确。")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise APIError(422, "review_notes_date_invalid", "复盘笔记日期格式不正确。") from None


def _query(owner_id, trade_day):
    return select(EvidenceVersion).where(
        EvidenceVersion.owner_id == owner_id, EvidenceVersion.visibility == "private",
        EvidenceVersion.source_id == MANUAL_SOURCE_ID,
        EvidenceVersion.source_key == notes_key(trade_day), EvidenceVersion.kind == "manual",
    )


def _head(db, owner_id, trade_day, cutoff=None):
    statement = _query(owner_id, trade_day)
    if cutoff is not None:
        statement = statement.where(EvidenceVersion.received_at <= cutoff)
    return db.scalar(statement.order_by(EvidenceVersion.version_number.desc(), EvidenceVersion.id.desc()).limit(1))


def _decode(row):
    try:
        if row.provenance != "manual":
            raise ValueError()
        text = row.payload.get("text")
        if not isinstance(text, str) or len(text) > 20000:
            raise ValueError()
        return ReviewNotesContent.model_validate_json(text)
    except (ValueError, TypeError, ValidationError, AttributeError):
        raise APIError(409, "review_notes_content_invalid", "该笔记版本格式不完整，原记录已保留。") from None


def _revision(row):
    return {"id": str(row.id), "version_number": row.version_number,
            "source_id": str(row.source_id), "source_key": row.source_key,
            "source_revision": row.source_revision,
            "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
            "received_at": row.received_at.isoformat(),
            "published_at": row.published_at.isoformat() if row.published_at else None,
            "content_hash": row.content_hash, "authorship": "original"}


def _reference_ids(content):
    return set(content.data_evidence_ids) | {key for item in content.lhb_notes for key in item.evidence_ids} | {
        item.evidence_id for item in content.catalyst_links
    }


def _visible_references(db, owner_id, identities, cutoff=None):
    if not identities:
        return {}
    # Private originals remain their owner's material even after source disable.
    # Shared cache needs current permission. No raw shared row is returned by
    # any notes read; these objects are used only for binding validation.
    statement = select(EvidenceVersion).join(Provider, Provider.id == EvidenceVersion.source_id).where(
        EvidenceVersion.id.in_(identities),
        or_(and_(EvidenceVersion.visibility == "private", EvidenceVersion.owner_id == owner_id),
            and_(EvidenceVersion.visibility == "shared", EvidenceVersion.owner_id.is_(None),
                 Provider.configured.is_(True), Provider.enabled.is_(True),
                 Provider.entitlement_status == "verified", Provider.rights["display"].astext == "allowed",
                 Provider.rights["cache"].astext == "allowed")),
    )
    if cutoff is not None:
        statement = statement.where(EvidenceVersion.received_at <= cutoff)
    return {row.id: row for row in db.scalars(statement)}


def _reference_status(db, owner_id, content, cutoff=None):
    ids = _reference_ids(content)
    visible = _visible_references(db, owner_id, ids, cutoff)
    return [{"evidence_id": str(key), "state": "available" if key in visible else "unavailable"}
            for key in sorted(ids)]


def _validate_bindings(db, owner_id, content, cutoff):
    ids = _reference_ids(content)
    visible = _visible_references(db, owner_id, ids, cutoff)
    if set(visible) != ids:
        raise APIError(422, "review_notes_evidence_unavailable", "引用材料不存在或当前不可访问，请移除失效引用后重试。")
    for link in content.catalyst_links:
        rows = visible[link.evidence_id].payload.get("rows")
        if not isinstance(rows, list) or link.source_row >= len(rows):
            raise APIError(422, "review_notes_source_row_invalid", "催化事件引用的原始行不存在。")


def _writable(provider):
    return bool(provider is not None and provider.kind == "manual" and provider.configured and provider.enabled
                and provider.rights.get("display") == "allowed" and provider.rights.get("cache") == "allowed")


def notes_view(db, owner_id, trade_day, revision_id=None, as_of_time=None):
    trade_day = _day(trade_day)
    if as_of_time is not None and (
        not isinstance(as_of_time, datetime) or as_of_time.utcoffset() is None or as_of_time > now(db)
    ):
        raise APIError(422, "review_notes_cutoff_invalid", "笔记查询截止时间需含时区且不晚于服务器当前时间。")
    head = _head(db, owner_id, trade_day, as_of_time)
    if revision_id is None:
        row = head
    else:
        statement = _query(owner_id, trade_day).where(EvidenceVersion.id == revision_id)
        if as_of_time is not None:
            statement = statement.where(EvidenceVersion.received_at <= as_of_time)
        row = db.scalar(statement)
        if row is None:
            raise APIError(404, "not_found", "该日期的笔记版本不存在或不可访问。")
    content = _decode(row) if row else ReviewNotesContent()
    is_current = bool(row is not None and head is not None and row.id == head.id)
    return {"trade_day": trade_day.isoformat(), "state": "complete" if row else "empty",
            "revision": _revision(row) if row else None, "content": content.model_dump(mode="json"),
            "is_current": is_current, "head_id": str(head.id) if head else None,
            "writable": as_of_time is None and (row is None or is_current)
            and _writable(db.get(Provider, MANUAL_SOURCE_ID)),
            "as_of_time": as_of_time.isoformat() if as_of_time else None,
            "evidence_status": _reference_status(db, owner_id, content, as_of_time)}


def notes_history(db, owner_id, trade_day):
    trade_day = _day(trade_day)
    rows = list(db.scalars(_query(owner_id, trade_day).order_by(
        EvidenceVersion.version_number.desc(), EvidenceVersion.id.desc()).limit(50)))
    contents = [_decode(row) for row in rows]
    identities = {key for content in contents for key in _reference_ids(content)}
    visible = _visible_references(db, owner_id, identities)
    writable = _writable(db.get(Provider, MANUAL_SOURCE_ID))
    head_id = str(rows[0].id) if rows else None
    return {"trade_day": trade_day.isoformat(), "head_id": head_id,
            "items": [{"trade_day": trade_day.isoformat(), "state": "complete", "revision": _revision(row),
                       "content": content.model_dump(mode="json"), "head_id": head_id,
                       "is_current": n == 0, "writable": n == 0 and writable, "as_of_time": None,
                       "evidence_status": [{"evidence_id": str(key), "state": "available" if key in visible else "unavailable"}
                                           for key in sorted(_reference_ids(content))]}
                      for n, (row, content) in enumerate(zip(rows, contents, strict=True))]}


def notes_days(db, owner_id, trade_days: list[date], as_of_time=None):
    """One bounded head query for at most 63 days; never expose journal prose.

    Apply receipt cutoff before DISTINCT ON so a later correction cannot hide
    the version available at the requested time. Missing days are omitted.
    """
    if not isinstance(trade_days, list) or len(trade_days) > 63:
        raise APIError(422, "review_notes_days_invalid", "每次最多查询63个日期的笔记阶段。")
    key_days = {notes_key(value): _day(value).isoformat() for value in trade_days}
    if as_of_time is not None and (
        not isinstance(as_of_time, datetime) or as_of_time.utcoffset() is None or as_of_time > now(db)
    ):
        raise APIError(422, "review_notes_cutoff_invalid", "笔记查询截止时间需含时区且不晚于服务器当前时间。")
    if not key_days:
        return {"state": "empty", "items": []}
    statement = select(EvidenceVersion).where(
        EvidenceVersion.owner_id == owner_id, EvidenceVersion.visibility == "private",
        EvidenceVersion.source_id == MANUAL_SOURCE_ID, EvidenceVersion.kind == "manual",
        EvidenceVersion.source_key.in_(key_days),
    )
    if as_of_time is not None:
        statement = statement.where(EvidenceVersion.received_at <= as_of_time)
    rows = list(db.scalars(statement.distinct(EvidenceVersion.source_key).order_by(
        EvidenceVersion.source_key, EvidenceVersion.version_number.desc(), EvidenceVersion.id.desc()).limit(63)))
    items = []
    for row in rows:
        content = _decode(row)
        items.append({"trade_day": key_days[row.source_key], "stage": content.stage,
                      "theme_stages": [item.model_dump(mode="json") for item in content.theme_stages],
                      "revision": _revision(row)})
    return {"state": "complete" if items else "empty", "items": items}


def save_notes(db, owner, body, request_id=None):
    """Append a journal revision; caller owns the transaction/idempotency key."""
    require_role(owner, "admin", "researcher")
    if not isinstance(body, ReviewNotesInput):
        body = ReviewNotesInput.model_validate(body)
    key = notes_key(body.trade_day)
    # Match import_evidence's lock so generic evidence imports cannot race CAS.
    lock(db, f"evidence:{owner.id}:{MANUAL_SOURCE_ID}:{key}")
    head = _head(db, owner.id, body.trade_day)
    if body.expected_head != (head.id if head else None):
        raise APIError(409, "review_notes_conflict", "笔记已有新版本，已保留原记录；请先读取最新版本再保存。")
    if head is not None:
        _decode(head)
    provider = db.scalar(select(Provider).where(Provider.id == MANUAL_SOURCE_ID).with_for_update(read=True))
    if not _writable(provider):
        raise APIError(422, "manual_source_required", "现有人工材料录入端口未启用或无保存与展示许可。")
    _validate_bindings(db, owner.id, body.content, now(db))
    evidence = EvidenceInput(source_id=MANUAL_SOURCE_ID, source_key=key,
                             source_revision=str(uuid.uuid4()), kind="manual", authorship="original",
                             payload=ManualPayload(text=encode_content(body.content)),
                             rights_basis="本人原创的每日复盘笔记；引用仅绑定原始证据，不代表外部数据或P2研究发布。",
                             supersedes_id=head.id if head else None)
    saved, _, _ = import_evidence(db, owner, evidence, request_id)
    return notes_view(db, owner.id, body.trade_day, revision_id=saved.id), "complete", []
