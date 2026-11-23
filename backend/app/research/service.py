from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, tuple_

from app.auth import APIError
from app.evidence.common import content_hash, normalized, now
from app.evidence.models import CalendarVersion, Snapshot, TradingSession
from app.evidence.service import audit
from app.models import Instrument
from app.research import schemas as s
from app.research.models import DocumentRevision, PlanScenario, PublicationEvent, ResearchDocument, RevisionLink


def publication_states(db, ids):
    states = {key: ("draft", None) for key in ids}
    rows = list(db.scalars(select(PublicationEvent).where(PublicationEvent.revision_id.in_(ids)))) if ids else []
    published = {item.revision_id: item.recorded_at for item in rows if item.kind == "published"}
    withdrawn = {item.revision_id for item in rows if item.kind == "withdrawn"}
    for key in ids:
        states[key] = ("withdrawn" if key in withdrawn else "published" if key in published else "draft", published.get(key))
    return states


def document_dtos(db, rows):
    if not rows:
        return []
    heads = {item.document_id: item for item in db.scalars(select(DocumentRevision).where(
        tuple_(DocumentRevision.document_id, DocumentRevision.revision_number).in_([(row.id, row.head_revision_number) for row in rows])))}
    states = publication_states(db, [row.id for row in heads.values()])
    return [s.Document(id=row.id, owner_id=row.owner_id, kind=row.kind, source_trade_day=row.source_trade_day,
        target_trade_day=row.target_trade_day, head_revision_id=heads[row.id].id, head_revision_number=row.head_revision_number,
        head_revision_title=heads[row.id].title, head_publication_status=states[heads[row.id].id][0], created_at=row.created_at) for row in rows]


def revision_input(db, row):
    parents = list(db.scalars(select(RevisionLink.parent_revision_id).where(RevisionLink.child_revision_id == row.id).order_by(RevisionLink.position)))
    return s.RevisionInput(title=row.title, snapshot_id=row.snapshot_id, calendar_version_id=row.calendar_version_id,
                           upstream_revision_ids=parents, content=row.content)


def revision_dto(db, row, document):
    body = revision_input(db, row)
    status, published = publication_states(db, [row.id])[row.id]
    scenarios = list(db.scalars(select(PlanScenario).where(PlanScenario.plan_revision_id == row.id).order_by(PlanScenario.position)))
    return s.Revision(**body.model_dump(), id=row.id, document_id=row.document_id, owner_id=row.owner_id, author_id=row.author_id,
        revision_number=row.revision_number, document_head_revision_number=document.head_revision_number,
        based_on_revision_id=row.based_on_revision_id, publication_status=status, published_at=published,
        recorded_at=row.recorded_at, content_hash=row.content_hash,
        scenarios=[s.Scenario(**item.payload, id=item.id, plan_revision_id=row.id) for item in scenarios])


def revision_summaries(db, rows):
    states = publication_states(db, [row.id for row in rows])
    return [s.RevisionSummary(id=row.id, document_id=row.document_id, revision_number=row.revision_number,
        based_on_revision_id=row.based_on_revision_id, title=row.title, snapshot_id=row.snapshot_id,
        calendar_version_id=row.calendar_version_id, publication_status=states[row.id][0], published_at=states[row.id][1],
        recorded_at=row.recorded_at, content_hash=row.content_hash) for row in rows]


def document_for_update(db, document_id, owner):
    row = db.scalar(select(ResearchDocument).where(ResearchDocument.id == document_id, ResearchDocument.owner_id == owner.id).with_for_update())
    if not row:
        raise APIError(404, "not_found", "研究文档不存在或不可访问。")
    return row


def match_head(document, expected):
    if document.head_revision_number != expected:
        raise APIError(412, "revision_conflict", "文档已有新版本，已保留你的输入；请先读取最新版本再处理冲突。")


def checked_calendar(db, document, calendar_id, *, required=False):
    if calendar_id is None:
        if required:
            raise APIError(422, "calendar_unavailable", "发布前需在新修订中明确绑定已导入的交易日历版本。")
        return
    calendar = db.get(CalendarVersion, calendar_id)
    day = db.get(TradingSession, (calendar_id, document.source_trade_day)) if calendar else None
    if not day or not day.is_open:
        raise APIError(422, "calendar_unavailable", "所选日历没有明确声明来源日开市，不能猜测交易日。")
    if document.kind == "plan":
        following = db.scalar(select(TradingSession.trade_day).where(TradingSession.calendar_id == calendar_id,
            TradingSession.trade_day > document.source_trade_day, TradingSession.is_open.is_(True)).order_by(TradingSession.trade_day).limit(1))
        if following is None:
            raise APIError(422, "calendar_unavailable", "日历覆盖内尚无后续开市日。")
        if following != document.target_trade_day:
            raise APIError(422, "invalid_target_day", "预案目标日必须是所选日历声明的下一开市日。")


def content_references(content):
    if isinstance(content, s.ReviewContent):
        return set(content.evidence_refs), set()
    rows = content.items if isinstance(content, s.DirectionContent) else content.scenarios
    evidence = {key for item in rows for key in item.evidence_refs}
    instruments = {key for item in rows for key in (item.core_instrument_ids if isinstance(item, s.DirectionItem) else item.instrument_ids)}
    return evidence, instruments


def validate_revision(db, document, body, recorded):
    if body.content.type != document.kind:
        raise APIError(422, "invalid_content", "研究内容类型必须与文档类型一致。")
    snapshot = db.scalar(select(Snapshot).where(Snapshot.id == body.snapshot_id, Snapshot.owner_id == document.owner_id))
    if not snapshot:
        raise APIError(409, "invalid_reference", "快照不存在或不可引用。")
    if snapshot.state != "ready" or snapshot.recorded_at > recorded:
        raise APIError(409, "invalid_state", "研究修订必须引用本人已完成的快照。")
    evidence, instruments = content_references(body.content)
    allowed = {UUID(item["evidence_version_id"]) for item in snapshot.manifest["items"]}
    if not evidence <= allowed:
        raise APIError(409, "invalid_reference", "引用的材料不是该快照中的精确版本。")
    if instruments and db.scalar(select(func.count()).select_from(Instrument).where(Instrument.id.in_(instruments))) != len(instruments):
        raise APIError(422, "instrument_unknown", "研究引用了尚未导入的标的资料。")
    if document.kind == "review":
        if body.upstream_revision_ids:
            raise APIError(409, "invalid_reference", "每日复盘不能引用方向或预案作为父研究。")
    else:
        if not body.upstream_revision_ids:
            raise APIError(409, "invalid_reference", "请明确引用至少一个已发布的上游研究修订。")
        parents = list(db.scalars(select(DocumentRevision).where(DocumentRevision.id.in_(body.upstream_revision_ids),
            DocumentRevision.owner_id == document.owner_id).order_by(DocumentRevision.id)))
        if len(parents) != len(body.upstream_revision_ids):
            raise APIError(409, "invalid_reference", "父研究不存在或不可引用。")
        expected_kind = "review" if document.kind == "direction" else "direction"
        parent_documents = {item.id: item for item in db.scalars(select(ResearchDocument).where(
            ResearchDocument.id.in_({parent.document_id for parent in parents})).order_by(ResearchDocument.id).with_for_update())}
        states = publication_states(db, [item.id for item in parents])
        for parent in parents:
            upstream = parent_documents[parent.document_id]
            status, published = states[parent.id]
            if (upstream.id == document.id or upstream.kind != expected_kind or upstream.source_trade_day != document.source_trade_day
                or status != "published" or published is None or parent.recorded_at > recorded or published > recorded):
                raise APIError(409, "invalid_reference", "父研究须为同来源日、已发布且未撤回的正确类型精确修订。")
    checked_calendar(db, document, body.calendar_version_id)
    return snapshot


def revision_quality(snapshot, body):
    quality = snapshot.manifest["quality"]
    status, reasons = quality["status"], list(quality["reasons"])
    if body.calendar_version_id is None:
        if status in {"complete", "empty"}:
            status = "partial"
        reasons.append("交易日历尚未绑定，只能保存草稿")
    return status, reasons


def append(db, document, owner, body, request_id, previous=None):
    recorded = now(db)
    snapshot = validate_revision(db, document, body, recorded)
    digest = content_hash({"document": {"kind": document.kind, "source_trade_day": document.source_trade_day,
        "target_trade_day": document.target_trade_day}, "revision": body})
    row = DocumentRevision(document_id=document.id, owner_id=owner.id, revision_number=previous.revision_number + 1 if previous else 1,
        based_on_revision_id=previous.id if previous else None, title=body.title, snapshot_id=body.snapshot_id,
        calendar_version_id=body.calendar_version_id, content=normalized(body.content), content_hash=digest, author_id=owner.id, recorded_at=recorded)
    db.add(row); db.flush()
    for position, parent in enumerate(body.upstream_revision_ids):
        db.add(RevisionLink(child_revision_id=row.id, parent_revision_id=parent, owner_id=owner.id, position=position,
                            relation="review_direction" if document.kind == "direction" else "direction_plan"))
    if isinstance(body.content, s.PlanContent):
        for position, item in enumerate(body.content.scenarios):
            db.add(PlanScenario(plan_revision_id=row.id, owner_id=owner.id, position=position, payload=normalized(item)))
    if previous:
        document.head_revision_number = row.revision_number
    audit(db, owner.id, "research.revision.create", request_id, "document_revision", row.id)
    db.flush()
    return row, snapshot


def create_document(db, owner, body, request_id):
    document = ResearchDocument(owner_id=owner.id, kind=body.kind, source_trade_day=body.source_trade_day,
        target_trade_day=body.target_trade_day, head_revision_number=1, created_at=now(db))
    db.add(document); db.flush()
    row, snapshot = append(db, document, owner, body.revision, request_id)
    quality, reasons = revision_quality(snapshot, body.revision)
    return document_dtos(db, [document])[0], quality, reasons


def create_revision(db, owner, document_id, body, expected, request_id):
    document = document_for_update(db, document_id, owner)
    match_head(document, expected)
    previous = db.scalar(select(DocumentRevision).where(DocumentRevision.document_id == document.id,
                                                       DocumentRevision.revision_number == document.head_revision_number))
    row, snapshot = append(db, document, owner, body, request_id, previous)
    quality, reasons = revision_quality(snapshot, body)
    return revision_dto(db, row, document), quality, reasons


def publish(db, owner, revision_id, expected, request_id):
    row = db.scalar(select(DocumentRevision).where(DocumentRevision.id == revision_id, DocumentRevision.owner_id == owner.id))
    if not row:
        raise APIError(404, "not_found", "研究修订不存在或不可访问。")
    document = document_for_update(db, row.document_id, owner)
    match_head(document, expected)
    if row.revision_number != document.head_revision_number or publication_states(db, [row.id])[row.id][0] != "draft":
        raise APIError(409, "invalid_state", "只能发布当前尚未发布的研究修订。")
    body = revision_input(db, row)
    snapshot = validate_revision(db, document, body, row.recorded_at)
    checked_calendar(db, document, row.calendar_version_id, required=True)
    recorded = now(db)
    if document.source_trade_day > recorded.astimezone(ZoneInfo("Asia/Shanghai")).date():
        raise APIError(422, "future_source_day", "来源交易日尚未发生，当前只能保存草稿。")
    quality = snapshot.manifest["quality"]
    if quality["status"] not in {"complete", "partial", "empty"}:
        raise APIError(409, "insufficient_evidence", "必需材料不可用，当前只能保存草稿。")
    if quality["status"] in {"partial", "empty"} and not body.content.limitations:
        raise APIError(422, "limitations_required", "材料受限或为空时，请在新修订明确记录限制后再发布人工研究。")
    db.add(PublicationEvent(revision_id=row.id, owner_id=owner.id, actor_id=owner.id, kind="published", recorded_at=recorded))
    audit(db, owner.id, "research.publish", request_id, "document_revision", row.id)
    db.flush()
    return revision_dto(db, row, document), quality["status"], quality["reasons"]
