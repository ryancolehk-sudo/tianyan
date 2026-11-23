from datetime import UTC, datetime, time
from uuid import UUID

from sqlalchemy import select

from app.auth import APIError
from app.evidence.common import content_hash, normalized, now
from app.evidence.models import EvidenceVersion, Snapshot, TradingSession
from app.evidence.service import audit
from app.observations.models import AuctionObservation
from app.observations.service import BEIJING, plan_reference
from app.research.service import publication_states
from app.validations import schemas as s
from app.validations.models import ScenarioValidation


def dto(db, row):
    successor = db.scalar(select(ScenarioValidation.id).where(ScenarioValidation.supersedes_id == row.id))
    return s.Validation.model_validate({**{key:getattr(row,key) for key in s.ValidationInput.model_fields},
        'id':row.id, 'owner_id':row.owner_id, 'recorded_at':row.recorded_at.astimezone(UTC),
        'content_hash':row.content_hash, 'superseded_by_id':successor})


def quality(row):
    return ('unavailable', ['材料不足，人工保留缺失原因，不作支持或反证判断。']) if row.outcome == 'inconclusive' else (
        'partial', ['人工核对原条件与精确材料，不等于规则认证、模拟成交或收益。'])


def create(db, owner, body, request_id):
    revision, document = plan_reference(db, owner, body.plan_revision_id, body.scenario_id, locked=True)
    recorded = now(db)
    session = db.get(TradingSession, (revision.calendar_version_id, document.target_trade_day))
    if not session or not session.is_open or not any(item['kind'] == 'continuous' for item in session.segments):
        raise APIError(422, 'calendar_unavailable', '原预案日历没有明确目标日交易时段，不能判断收盘时点。')
    close = datetime.combine(document.target_trade_day, max(time.fromisoformat(item['end']) for item in session.segments), BEIJING)
    if recorded < close:
        raise APIError(422, 'before_close', '目标交易日尚未收盘，暂不能保存次日验证。')
    status, published = publication_states(db, [revision.id])[revision.id]
    if status != 'published' or published is None or published >= close:
        raise APIError(409, 'invalid_reference', '原预案必须在目标日收盘前已发布且未撤回。')
    snapshot = db.scalar(select(Snapshot).where(Snapshot.id == body.snapshot_id, Snapshot.owner_id == owner.id))
    if snapshot is None:
        raise APIError(409, 'invalid_reference', '验证快照不存在或不可引用。')
    if snapshot.state != 'ready' or snapshot.recorded_at > recorded:
        raise APIError(409, 'invalid_state', '验证必须引用本人已完成的冻结快照。')
    if snapshot.id == revision.snapshot_id or snapshot.as_of_time < close or snapshot.as_of_time > recorded:
        raise APIError(422, 'invalid_snapshot_time', '请使用目标日收盘后或之后的新快照，不能沿用预案快照或未来快照。')
    if body.observation_id is not None and db.scalar(select(AuctionObservation.id).where(
            AuctionObservation.id == body.observation_id, AuctionObservation.owner_id == owner.id,
            AuctionObservation.plan_revision_id == revision.id, AuctionObservation.scenario_id == body.scenario_id,
            AuctionObservation.recorded_at <= recorded)) is None:
        raise APIError(409, 'invalid_reference', '所选观察不属于本人这个精确预案与情景。')
    allowed = {UUID(item['evidence_version_id']) for item in snapshot.manifest['items']}
    if not set(body.evidence_refs) <= allowed:
        raise APIError(409, 'invalid_reference', '验证材料须来自所选快照的精确清单。')
    if body.outcome != 'inconclusive' and snapshot.manifest['quality']['status'] not in {'complete','partial'}:
        raise APIError(409, 'insufficient_evidence', '快照材料不足，只能保存不可判断及缺失原因。')
    rows = list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.id.in_(body.evidence_refs))))
    if len(rows) != len(body.evidence_refs) or any(not row.historical_eligible or row.provenance == 'unverified' or row.availability != 'known' or row.available_at is None or
            row.available_at > snapshot.as_of_time or row.received_at > snapshot.as_of_time or
            (row.published_at is not None and row.published_at > snapshot.as_of_time) or
            (row.event_at is not None and row.event_at > snapshot.as_of_time) for row in rows):
        raise APIError(422, 'late_evidence', '验证材料时点未知或晚于快照截止，不能作为该时点的结论证据。')
    previous = db.scalar(select(ScenarioValidation).where(ScenarioValidation.owner_id == owner.id,
        ScenarioValidation.plan_revision_id == revision.id, ScenarioValidation.scenario_id == body.scenario_id,
        ~ScenarioValidation.id.in_(select(ScenarioValidation.supersedes_id).where(ScenarioValidation.supersedes_id.is_not(None)))))
    if (previous.id if previous else None) != body.supersedes_id or previous and recorded <= previous.recorded_at:
        raise APIError(409, 'validation_conflict', '此情景已有验证或更正已更新。请查看当前原件后明确追加更正，输入已保留。')
    row = ScenarioValidation(owner_id=owner.id, **normalized(body))
    for key in ['plan_revision_id','scenario_id','snapshot_id','observation_id','supersedes_id']:
        setattr(row, key, getattr(body,key))
    row.recorded_at = recorded; row.content_hash = content_hash({'input':body,'recorded_at':recorded})
    db.add(row); db.flush()
    audit(db, owner.id, 'research.validation.create', request_id, 'validation', row.id)
    return dto(db, row), *quality(row)
