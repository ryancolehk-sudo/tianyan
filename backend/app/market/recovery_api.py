from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import principal
from app.db import db_session
from app.evidence.common import now
from app.http import envelope
from app.market.recovery_models import RecoveryCoverage
from app.market.resilience_models import DataHealth
from app.schemas import DTO, Envelope
from app.models import WorkerHeartbeat

router = APIRouter()


class RecoveryItem(DTO):
    id: UUID
    source_id: UUID
    dataset: str
    scope: dict
    trade_day: date
    interval: str
    state: str
    expected_count: int | None
    actual_count: int | None
    missing_ranges: list[dict]
    job_id: UUID | None
    reason: str | None
    checked_at: AwareDatetime
    next_retry_at: AwareDatetime | None


class RecoveryStatus(DTO):
    server_time: AwareDatetime
    items: list[RecoveryItem]
    has_more: bool = False
    counts: dict[str, int] = Field(default_factory=dict)
    workers: list[dict] = Field(default_factory=list)
    health_state: str | None = None
    health: list['HealthItem'] = Field(default_factory=list)


class HealthItem(DTO):
    id: UUID
    capability: str
    scope: dict
    trade_day: date
    state: str
    stage: str
    primary_source_id: UUID | None
    active_source_id: UUID | None
    last_valid_at: AwareDatetime | None
    last_source_time: AwareDatetime | None
    reason: str | None
    next_action: str | None
    next_action_at: AwareDatetime | None
    missing_count: int | None
    updated_at: AwareDatetime
    impact_modules: list[str] = Field(default_factory=lambda: ['工作台', '每日复盘'])


RecoveryStatus.model_rebuild()


@router.get('/api/v1/data-recovery', response_model=Envelope[RecoveryStatus], operation_id='getDataRecovery')
def status(request: Request, day: date | None = None, source_id: UUID | None = None,
           limit: int = Query(default=100, ge=1, le=500),
           offset: int = Query(default=0, ge=0, le=1000000),
           identity=Depends(principal), db: Session = Depends(db_session)):
    # Reads only: neither a browser refresh nor a monitoring check queues work.
    query = select(RecoveryCoverage)
    if identity[0].role != 'admin':
        query = query.where(RecoveryCoverage.owner_id == identity[0].id)
    if day is not None:
        query = query.where(RecoveryCoverage.trade_day == day)
    if source_id is not None:
        query = query.where(RecoveryCoverage.source_id == source_id)
    count_query=query.with_only_columns(RecoveryCoverage.state,func.count()).group_by(RecoveryCoverage.state)
    counts=dict(db.execute(count_query).all())
    rows = list(db.scalars(query.order_by(RecoveryCoverage.trade_day.desc(),
        RecoveryCoverage.dataset, RecoveryCoverage.id).offset(offset).limit(limit+1)))
    workers=[]
    if identity[0].role == 'admin':
        for worker in db.scalars(select(WorkerHeartbeat).where(WorkerHeartbeat.worker_id.startswith('market:'))):
            workers.append(dict(role=worker.worker_id.split(':')[1], seen_at=worker.seen_at,
                progress_at=worker.progress_at, job_id=worker.job_id))
    clock = now(db)
    from app.market.protocol import SHANGHAI
    health_query = select(DataHealth).where(DataHealth.trade_day == (day or clock.astimezone(SHANGHAI).date()))
    if source_id is not None:
        health_query = health_query.where(or_(DataHealth.primary_source_id == source_id,
                                             DataHealth.active_source_id == source_id))
    if identity[0].role != 'admin':
        health_query = health_query.where(DataHealth.owner_id == identity[0].id)
    health_rows = list(db.scalars(health_query.order_by(DataHealth.updated_at.desc(), DataHealth.id).limit(500)))
    from app.models import Provider
    from app.market.models import SourceSettings
    from app.market.minute_backfill import configuration_fingerprint
    current = {}
    for provider, configuration in db.execute(select(Provider,SourceSettings).join(SourceSettings,
        SourceSettings.source_id==Provider.id)):
        current[provider.id] = (provider.enabled,configuration.owner_id,configuration_fingerprint(provider,configuration))
    health_rows = [row for row in health_rows if row.primary_source_id is None or
        row.primary_source_id in current and current[row.primary_source_id]==(True,row.owner_id,row.configuration_sha256)]
    health_items = [HealthItem.model_validate(row) for row in health_rows]
    from datetime import timedelta
    from app.config import settings
    if settings().data_collection_enabled:
        for item in health_items:
            if item.state!='attention' and item.trade_day==clock.astimezone(SHANGHAI).date() and clock-item.updated_at>timedelta(seconds=60):
                item.state,item.stage,item.reason='recovering','monitor','health_check_overdue'
                item.next_action,item.next_action_at='repair_queue',clock
    levels = {'normal':0, 'fallback':1, 'recovering':2, 'attention':3}
    health_state = max((row.state for row in health_items), key=levels.get, default=None)
    if not settings().data_collection_enabled:
        health_state = None
    data = RecoveryStatus(server_time=clock, items=[RecoveryItem.model_validate(row) for row in rows[:limit]],
        has_more=len(rows)>limit, counts=counts, workers=workers, health_state=health_state,
        health=health_items)
    return envelope(request, data, 'partial' if rows else 'empty',
        ['覆盖记录来自本机对账；分钟空段不等于已证明丢失成交，补录不会改写原接收时间。'])
