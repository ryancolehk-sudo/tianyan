from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.auth import APIError
from app.config import settings
from app.db import engine
from app.evidence import schemas as s
from app.evidence.common import content_hash, lock, normalized, now
from app.evidence.models import CalendarVersion, EvidenceVersion, Snapshot, SnapshotItem, SourceHealth, SourceVersion, TradingSession
from app.models import AuditEvent, Instrument, Job, Provider
from app.schemas import Provider as ProviderDTO


def audit(db, owner_id, action, request_id, resource_type, resource_id):
    db.add(AuditEvent(actor_id=owner_id, action=action, request_id=request_id,
                     resource_type=resource_type, resource_id=resource_id, changed_field_names=["created"]))


def evidence_dtos(db, rows):
    if not rows:
        return []
    scopes = or_(*[and_(EvidenceVersion.source_id == item.source_id, EvidenceVersion.owner_id == item.owner_id,
                       EvidenceVersion.source_key == item.source_key) for item in rows])
    heads = {(source, owner, key): number for source, owner, key, number in db.execute(
        select(EvidenceVersion.source_id, EvidenceVersion.owner_id, EvidenceVersion.source_key, func.max(EvidenceVersion.version_number))
        .where(scopes).group_by(EvidenceVersion.source_id, EvidenceVersion.owner_id, EvidenceVersion.source_key))}
    return [s.EvidenceVersion.model_validate(item).model_copy(update={"is_current": heads[(item.source_id, item.owner_id, item.source_key)] == item.version_number}) for item in rows]


def import_evidence(db, owner, body, request_id):
    provider = db.get(Provider, body.source_id)
    if not provider or provider.kind != "manual" or not provider.configured or not provider.enabled:
        raise APIError(422, "manual_source_required", "请选择已启用的人工材料录入端口。")
    if provider.rights.get("cache") != "allowed" or provider.rights.get("display") != "allowed":
        raise APIError(403, "permission_missing", "当前材料源没有保存与展示许可。")
    lock(db, f"evidence:{owner.id}:{body.source_id}:{body.source_key}")
    digest = content_hash(body)
    scope = (EvidenceVersion.owner_id == owner.id, EvidenceVersion.source_id == body.source_id,
             EvidenceVersion.source_key == body.source_key)
    duplicate = db.scalar(select(EvidenceVersion).where(*scope, EvidenceVersion.source_revision == body.source_revision))
    if duplicate:
        if duplicate.content_hash != digest:
            raise APIError(409, "source_revision_conflict", "同一材料版本内容不一致，原记录已保留；请明确创建更正版本。")
        return evidence_dtos(db, [duplicate])[0], "complete" if duplicate.historical_eligible else "partial", ["相同材料版本已存在，未重复保存"]
    previous = db.scalars(select(EvidenceVersion).where(*scope)).all()
    if previous:
        head = max(previous, key=lambda item: item.version_number)
        if not head or body.supersedes_id != head.id or body.kind != head.kind or body.instrument_id != head.instrument_id:
            raise APIError(409, "invalid_reference", "更正必须引用本人同一材料当前版本，不能分叉或改变材料类型。")
    elif body.supersedes_id:
        raise APIError(409, "invalid_reference", "找不到可更正的本人材料。")
    instrument_ids = [body.instrument_id] if body.instrument_id else []
    if isinstance(body.payload, s.ClassificationPayload):
        instrument_ids += body.payload.instrument_ids
    if instrument_ids and db.scalar(select(func.count()).select_from(Instrument).where(Instrument.id.in_(set(instrument_ids)))) != len(set(instrument_ids)):
        raise APIError(422, "instrument_unknown", "关联标的资料尚未导入，不能引用未知标的。")
    received = now(db)
    original = body.authorship == "original" and body.kind == "manual"
    published = received if original else body.published_at
    row = EvidenceVersion(**body.model_dump(exclude={"payload", "published_at", "authorship"}), owner_id=owner.id, visibility="private",
        version_number=head.version_number + 1 if previous else 1,
        payload=normalized(body.payload), published_at=published, received_at=received,
        available_at=max(received, published) if published else None, availability="known" if published else "unknown",
        provenance="manual" if original else "unverified", historical_eligible=original, content_hash=digest)
    db.add(row); db.flush()
    db.add(SourceHealth(owner_id=owner.id, source_id=provider.id, checked_at=received, status="ready",
        last_event_at=body.event_at, last_received_at=received,
        coverage={"expected": 1, "accepted": 1, "duplicates": 0}, reason="人工材料保存成功，不代表外部行情源可用"))
    audit(db, owner.id, "evidence.import", request_id, "evidence_version", row.id)
    return evidence_dtos(db, [row])[0], "complete" if original else "partial", [] if original else ["手工导入的外部材料尚未核验，不进入严格历史快照"]


def import_calendar(db, owner, body, request_id):
    lock(db, f"calendar:{body.exchange}:{body.version}")
    digest = content_hash(body)
    duplicate = db.scalar(select(CalendarVersion).where(CalendarVersion.exchange == body.exchange, CalendarVersion.version == body.version))
    if duplicate:
        if duplicate.content_hash != digest:
            raise APIError(409, "calendar_version_conflict", "该日历版本已存在且内容不同，请创建新版本。")
        return s.CalendarVersion.model_validate(duplicate), "partial", ["相同版本已存在，未重复导入；日历由本机维护者提供"]
    row = CalendarVersion(**body.model_dump(exclude={"sessions"}), recorded_at=now(db), author_id=owner.id, content_hash=digest)
    db.add(row); db.flush()
    db.add_all(TradingSession(calendar_id=row.id, **item.model_dump(exclude={"segments"}), segments=normalized(item.segments)) for item in body.sessions)
    audit(db, owner.id, "calendar.import", request_id, "calendar_version", row.id)
    return s.CalendarVersion.model_validate(row), "partial", ["人工导入日历，未由本系统核验交易所来源"]


def session_lookup(db, exchange, version, day):
    calendar = db.scalar(select(CalendarVersion).where(CalendarVersion.exchange == exchange, CalendarVersion.version == version))
    if not calendar:
        return s.CalendarLookup(calendar_id=None, exchange=exchange, requested_day=day, calendar_version=version,
                                session=None, reason="日历版本尚未导入")
    row = db.get(TradingSession, (calendar.id, day))
    return s.CalendarLookup(calendar_id=calendar.id, exchange=exchange, requested_day=day, calendar_version=version,
        session=s.SessionInput.model_validate(row) if row else None, reason=None if row else "查询日期超出已声明覆盖范围")


def snapshot_selection(owner_id, body):
    from app.provider_access import cloud_share_allowed
    # Close the repeatable-read connection before acquiring the write connection:
    # one pool slot per request, and late worker execution cannot widen this set.
    with Session(engine().execution_options(isolation_level="REPEATABLE READ")) as db, db.begin():
        if body.as_of_time > now(db):
            raise APIError(422, "future_cutoff", "快照截止时间不能晚于服务器当前时间。")
        providers = list(db.scalars(select(Provider).order_by(Provider.id)))
        versions = list(db.scalars(select(SourceVersion).where(SourceVersion.recorded_at <= body.as_of_time)
                                  .order_by(SourceVersion.recorded_at.desc(), SourceVersion.id.desc())))
        selected_sources = {}
        for version in versions:
            selected_sources.setdefault(version.source_id, version)
        sources = [normalized(s.SnapshotSource(version_id=item.id, recorded_at=item.recorded_at,
                   content_hash=item.content_hash, descriptor=item.descriptor)) for _, item in sorted(selected_sources.items())]
        historical = [ProviderDTO.model_validate(item.descriptor) for item in selected_sources.values()]
        current_permission = {item.id for item in providers if item.enabled and item.configured and item.entitlement_status == "verified" and
                      item.rights.get("display") == "allowed" and item.rights.get("cache") == "allowed" and cloud_share_allowed(item)}
        permission = {item.id for item in historical if item.enabled and item.configured and item.entitlement_status == "verified"
                      and item.rights.display == "allowed" and item.rights.cache == "allowed" and item.id in current_permission}
        scope = or_(and_(EvidenceVersion.visibility == "private", EvidenceVersion.owner_id == owner_id), EvidenceVersion.visibility == "shared")
        if body.source_keys is not None:
            scope = and_(scope, EvidenceVersion.source_key.in_(body.source_keys))
        candidates = list(db.scalars(select(EvidenceVersion).where(scope, EvidenceVersion.source_id.in_(permission),
            EvidenceVersion.received_at <= body.as_of_time, EvidenceVersion.historical_eligible.is_(True),
            EvidenceVersion.available_at <= body.as_of_time,
            or_(EvidenceVersion.event_at.is_(None), EvidenceVersion.event_at <= body.as_of_time))
            .order_by(EvidenceVersion.version_number.desc(), EvidenceVersion.id).limit(20001)))
        if len(candidates) > 20000:
            raise APIError(422, "snapshot_limit", "本次材料版本超过快照范围上限，请先缩小研究材料范围。")
        # First apply the cutoff, then choose the server-ordered revision per scope.
        latest = {}
        for item in candidates:
            latest.setdefault((item.source_id, item.owner_id, item.source_key), item)
        items = sorted([{"evidence_version_id": str(item.id), "content_hash": item.content_hash}
                        for item in latest.values()], key=lambda item: item["evidence_version_id"])
        excluded = db.scalar(select(func.count()).select_from(EvidenceVersion).where(scope,
            EvidenceVersion.received_at <= body.as_of_time, EvidenceVersion.historical_eligible.is_(False)))
        capabilities = {cap for item in historical if item.id in permission for cap in item.capabilities}
        missing = sorted(set(body.required_capabilities) - capabilities)
        reasons = (["缺少必需能力：" + "、".join(missing)] if missing else [])
        if excluded:
            reasons.append("存在未核验/发布时间未知材料，已排除严格快照")
        quality = "unavailable" if missing or (excluded and not items) else "partial" if excluded else "complete" if items else "empty"
        return {"format": "tianyan.snapshot.v1", "app_mode": settings().app_mode, "owner_id": str(owner_id),
            "as_of_time": normalized(body.as_of_time), "knowledge_mode": "observed", "required_capabilities": body.required_capabilities,
            "source_keys": body.source_keys,
            "sources": sources, "quality": {"status": quality, "reasons": reasons}, "items": items}


def queue_snapshot(db, owner, selection, request_id):
    row = Snapshot(owner_id=owner.id, as_of_time=datetime.fromisoformat(selection["as_of_time"].replace("Z", "+00:00")),
                   recorded_at=now(db), state="pending", knowledge_mode="observed", selection=selection)
    db.add(row); db.flush()
    db.add_all(SnapshotItem(snapshot_id=row.id, evidence_version_id=item["evidence_version_id"], content_hash=item["content_hash"])
               for item in selection["items"])
    job = Job(owner_id=owner.id, kind="snapshot.create", payload={"snapshot_id": str(row.id)})
    db.add(job); db.flush()
    audit(db, owner.id, "snapshot.request", request_id, "snapshot", row.id)
    return {"job_id": job.id, "result_ref": {"type": "snapshot", "id": row.id}}, "partial", ["快照正在冻结，尚不可作为已完成材料引用"]


def snapshot_dto(row):
    return s.Snapshot(id=row.id, owner_id=row.owner_id, state=row.state, as_of_time=row.as_of_time,
        recorded_at=row.recorded_at, knowledge_mode=row.knowledge_mode, manifest_hash=row.manifest_hash,
        quality=row.selection["quality"] if row.state == "ready" else {"status": "unavailable", "reasons": ["快照尚未冻结" if row.state == "pending" else "快照冻结失败"]},
        manifest=row.manifest, items=row.manifest["items"] if row.manifest else [], error_code=row.error_code)


def freeze_snapshot(job):
    with Session(engine()) as db:
        row = db.scalar(select(Snapshot).where(Snapshot.id == job.payload.get("snapshot_id"), Snapshot.owner_id == job.owner_id))
        if not row:
            raise APIError(409, "snapshot_missing", "快照不存在。")
        manifest = normalized(s.Manifest.model_validate(row.selection))
        digest = content_hash(manifest)
    # Atomically fence both the material result and task success, after computation.
    with Session(engine()) as db, db.begin():
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == "running", Job.lease_token == job.lease_token,
                                             Job.lease_until > func.clock_timestamp()).with_for_update())
        if not current:
            return False
        snapshot = db.scalar(select(Snapshot).where(Snapshot.id == job.payload["snapshot_id"], Snapshot.owner_id == job.owner_id).with_for_update())
        if snapshot.state != "pending":
            raise APIError(409, "snapshot_state", "快照状态不允许再次冻结。")
        persisted = list(db.execute(select(SnapshotItem.evidence_version_id, SnapshotItem.content_hash).where(SnapshotItem.snapshot_id == snapshot.id)))
        if sorted((str(item[0]), item[1]) for item in persisted) != sorted((item["evidence_version_id"], item["content_hash"]) for item in manifest["items"]):
            raise APIError(409, "snapshot_manifest_mismatch", "快照材料清单不一致，冻结未完成。")
        snapshot.manifest, snapshot.manifest_hash, snapshot.state = manifest, digest, "ready"
        current.state, current.completed_at = "succeeded", now(db)
        current.result_ref = {"type": "snapshot", "id": str(snapshot.id)}
        current.lease_token = current.lease_until = None
        audit(db, job.owner_id, "snapshot.frozen", None, "snapshot", snapshot.id)
    return True


def fail_snapshot(job, code):
    with Session(engine()) as db, db.begin():
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == "running", Job.lease_token == job.lease_token,
                                             Job.lease_until > func.clock_timestamp()).with_for_update())
        if not current:
            return False
        row = db.scalar(select(Snapshot).where(Snapshot.id == job.payload.get("snapshot_id"), Snapshot.owner_id == job.owner_id).with_for_update())
        if row and row.state == "pending":
            row.state, row.error_code = "failed", code
        current.state, current.error_code, current.completed_at = "failed", code, now(db)
        current.lease_token = current.lease_until = None
    return True
