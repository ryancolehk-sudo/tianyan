from datetime import UTC, time
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.auth import APIError
from app.evidence.common import content_hash, normalized, now
from app.evidence.models import EvidenceVersion, Snapshot, TradingSession
from app.evidence.service import audit
from app.observations import schemas as s
from app.observations.models import AuctionObservation
from app.research.models import DocumentRevision, PlanScenario, ResearchDocument
from app.research.service import publication_states

BEIJING = ZoneInfo('Asia/Shanghai')


def plan_reference(db, owner, revision_id, scenario_id=None, *, locked=False):
    stmt = select(DocumentRevision, ResearchDocument).join(ResearchDocument, ResearchDocument.id == DocumentRevision.document_id).where(
        DocumentRevision.id == revision_id, DocumentRevision.owner_id == owner.id, ResearchDocument.kind == 'plan')
    if locked:
        stmt = stmt.with_for_update(of=ResearchDocument)
    pair = db.execute(stmt).first()
    if pair is None:
        raise APIError(404, 'not_found', '预案版本不存在或不可访问。')
    if scenario_id is not None and db.scalar(select(PlanScenario.id).where(PlanScenario.id == scenario_id,
            PlanScenario.plan_revision_id == revision_id, PlanScenario.owner_id == owner.id)) is None:
        raise APIError(409, 'invalid_reference', '情景不属于这个精确预案版本。')
    return pair


def dto(row):
    return s.Observation.model_validate({**{key: getattr(row, key) for key in s.ObservationInput.model_fields},
        'id': row.id, 'owner_id': row.owner_id, 'observed_at': row.observed_at.astimezone(UTC), 'recorded_at': row.recorded_at.astimezone(UTC), 'phase': row.phase,
        'timeliness': 'late' if row.recorded_at > row.observed_at else 'on_time', 'content_hash': row.content_hash})


def quality(row):
    if row.status == 'unavailable':
        return 'unavailable', ['该观察检查点缺少可核对材料，不能判断条件是否满足。']
    return 'partial', ['人工抄录仅保留精确材料引用，不等于供应商事实核验。'] + (['晚补记录，不代表当时已经保存的决策。'] if row.recorded_at > row.observed_at else [])


def create(db, owner, body, request_id):
    revision, document = plan_reference(db, owner, body.plan_revision_id, body.scenario_id, locked=True)
    recorded = now(db)
    status, published = publication_states(db, [revision.id])[revision.id]
    if status != 'published' or published is None or published >= body.observed_at:
        raise APIError(409, 'invalid_reference', '预案必须在观察时点之前已发布且未撤回，不能用事后预案回填。')
    local = body.observed_at.astimezone(BEIJING)
    phase = local.strftime('%H:%M')
    if local.date() != document.target_trade_day or local.second or local.microsecond or phase not in {'09:15', '09:20', '09:25'}:
        raise APIError(422, 'invalid_observation_time', '观察时点须为预案目标日北京时间09:15、09:20或09:25整分检查点。')
    if body.observed_at > recorded:
        raise APIError(422, 'future_observation', '观察检查点尚未到达，不能提前记录事实或缺失。')
    session = db.get(TradingSession, (revision.calendar_version_id, document.target_trade_day))
    if not session or not session.is_open or not any(item['kind'] == 'auction' and
            time.fromisoformat(item['start']) <= local.time() <= time.fromisoformat(item['end']) for item in session.segments):
        raise APIError(422, 'calendar_unavailable', '原预案日历未明确声明覆盖该检查点的竞价时段，请先补充日历并发布新预案版本。')
    snapshot = db.scalar(select(Snapshot).where(Snapshot.id == body.snapshot_id, Snapshot.owner_id == owner.id))
    if snapshot is None:
        raise APIError(409, 'invalid_reference', '观察快照不存在或不可引用。')
    if snapshot.state != 'ready' or snapshot.recorded_at > recorded:
        raise APIError(409, 'invalid_state', '观察必须引用本人已完成的冻结快照。')
    if snapshot.id == revision.snapshot_id or snapshot.as_of_time < body.observed_at or snapshot.as_of_time > recorded or snapshot.as_of_time.astimezone(BEIJING).date() != document.target_trade_day:
        raise APIError(422, 'invalid_snapshot_time', '观察须使用目标日的新快照，截止不能早于检查点或晚于当前时间。')
    allowed = {UUID(item['evidence_version_id']) for item in snapshot.manifest['items']}
    if not set(body.evidence_refs) <= allowed:
        raise APIError(409, 'invalid_reference', '事实材料须来自这个快照的精确清单。')
    if body.status == 'observed':
        if snapshot.manifest['quality']['status'] not in {'complete', 'partial'}:
            raise APIError(409, 'insufficient_evidence', '快照材料不可用或为空，只能记录缺失，不能填为观察事实。')
        rows = list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.id.in_(body.evidence_refs))))
        if len(rows) != len(body.evidence_refs) or any(not row.historical_eligible or row.provenance == 'unverified' or row.availability != 'known' or row.available_at is None or
                row.available_at > body.observed_at or row.received_at > body.observed_at or
                (row.published_at is not None and row.published_at > body.observed_at) or
                (row.event_at is not None and row.event_at > body.observed_at) for row in rows):
            raise APIError(422, 'late_evidence', '材料在检查点之后才可得，不能回填为当时已知事实。')
    row = AuctionObservation(owner_id=owner.id, **normalized(body))
    # UUID/time objects stay typed for the DB; canonical JSON is used only for JSON fields/hash.
    for key in ['plan_revision_id', 'scenario_id', 'snapshot_id', 'observed_at']:
        setattr(row, key, getattr(body, key))
    row.recorded_at = recorded; row.phase = phase; row.content_hash = content_hash({'input': body, 'recorded_at': recorded, 'phase': phase})
    db.add(row); db.flush()
    audit(db, owner.id, 'auction.observation.create', request_id, 'auction_observation', row.id)
    return dto(row), *quality(row)
