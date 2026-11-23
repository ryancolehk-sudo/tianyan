"""Direction page reads and explicitly requested, audited source acquisition."""
import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime
from sqlalchemy.orm import Session

from app.auth import APIError, csrf, principal, require_role
from app.db import db_session
from app.evidence.api import business_key
from app.evidence.common import now, write_once
from app.http import envelope
from app.market.direction_read import direction_view
from app.market.direction_specs import PUBLIC_FAMILIES
from app.market.review_api import check_cutoff
from app.market.review_jobs import batch_status, queue_dataset
from app.market.review_store import available_trade_days, family_id
from app.market.schemas import SourceConfiguration
from app.market.service import configure_source, enqueue_collection
from app.models import AuditEvent, Provider
from app.schemas import DTO, Envelope, Error

router = APIRouter()
ERRORS = {code: {'model': Error} for code in (401, 403, 404, 409, 422, 429, 503)}


class DirectionCollection(DTO):
    trade_day: date | None = None
    end_day: date | None = None
    lookback: Literal[5, 20, 60] = 5
    source_scope: Literal['four_sources', 'legacy'] = 'legacy'


def ensure_sources(db, owner, request_id, families=None):
    """Only an explicit admin request adds this feature's six configurations."""
    from app.market.registry import SOURCES
    for family in sorted((PUBLIC_FAMILIES - {'ths_industry_funds','ths_concept_funds','direction_kpl_top'} if families is None else families) | {'tushare_direction_industry', 'tushare_direction_industry_funds'}):
        if db.get(Provider, family_id(family)) is not None:
            continue
        configure_source(db, owner, SourceConfiguration(family=family, symbols=[],
            interval_seconds=SOURCES[family].minimum_interval,
            rights_basis='用户2026-10-26明确授权本机个人研究接入方向页板块及公开榜单；仅展示和缓存，禁止再分发。',
            rights={'display': 'allowed', 'cache': 'allowed', 'export': 'denied', 'redistribute': 'denied'},
            entitlement_status='verified', enabled=True), request_id)


@router.get('/api/v1/direction-data', response_model=Envelope[dict], responses=ERRORS,
            operation_id='getDirectionData')
def read_direction(request: Request, trade_day: date | None = None,
                   sector_type: Literal['concept', 'industry'] = 'concept',
                   end_day: date | None = None, as_of_time: AwareDatetime | None = None,
                   automatic_current: bool = False,
                   lookback: Literal['5', '20', '60'] = '5',
                   sector_code: str | None = Query(default=None, pattern=r'^BK\d{4,8}\.DC$'),
                   identity=Depends(principal), db: Session = Depends(db_session)):
    check_cutoff(db, as_of_time)
    if end_day is not None and trade_day is not None and end_day > trade_day:
        raise APIError(422, 'invalid_range', '历史窗口不能晚于研究日期')
    if lookback != '5':
        from app.read_cache import cached_endpoint
        return cached_endpoint(request, db, identity[0], 'review-rotation',
            dict(trade_day=end_day or trade_day, sector_type=sector_type, sector_code=sector_code,
                 lookback=int(lookback), as_of_time=as_of_time))
    value = direction_view(db, identity[0].id, trade_day=trade_day,
        sector_type=sector_type, end_day=end_day, as_of_time=as_of_time, automatic_current=automatic_current)
    return envelope(request, value, 'partial', ['各榜按实际资料日期和来源独立读取；采集时间不替代行情时间。'], as_of_time=as_of_time)


@router.get('/api/v1/direction-data/sector-atlas', response_model=Envelope[dict],
            responses=ERRORS, operation_id='getDirectionSectorAtlas')
def read_sector_atlas(request: Request, trade_day: date | None = None,
                      lookback: int = Query(default=20, ge=1, le=50),
                      source: Literal['kpl', 'ths'] = 'kpl',
                      selected_codes: str | None = Query(default=None, max_length=1000, pattern=r'^\d{6}(?:\.TI)?(?:,\d{6}(?:\.TI)?)*$'),
                      as_of_time: AwareDatetime | None = None,
                      identity=Depends(principal), db: Session = Depends(db_session)):
    check_cutoff(db, as_of_time)
    from app.read_cache import cached_endpoint
    return cached_endpoint(request,db,identity[0],'review-sector-atlas',
        dict(trade_day=trade_day,lookback=lookback,source=source,as_of_time=as_of_time,selected_codes=selected_codes))


@router.post('/api/v1/direction-data/collections', response_model=Envelope[dict],
             status_code=202, responses=ERRORS, operation_id='collectDirectionData')
def collect_direction(body: DirectionCollection, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin')
    def queue(db):
        from app.config import settings
        if not settings().data_collection_enabled:
            raise APIError(422, 'collection_disabled', '本机采集尚未启用')
        public = PUBLIC_FAMILIES - {'direction_kpl_top'}
        if body.source_scope != 'legacy':
            public = public - {'direction_kpl_strength'}
        ensure_sources(db, identity[0], request.state.request_id, public)
        days = available_trade_days(db, limit=200, end_day=body.trade_day, as_of_time=now(db))
        if not days or (body.trade_day and days[-1] != body.trade_day):
            raise APIError(422, 'calendar_not_ready', '所选日期尚无已核实收盘交易日历')
        day, batch_id, jobs, skipped = days[-1], uuid.uuid4(), [], []
        if body.end_day is not None and body.end_day > day:
            raise APIError(422, 'invalid_range', '历史窗口不能晚于研究日期')
        window = [value for value in days if value <= (body.end_day or day)][-body.lookback:]
        previous = next((value for value in reversed(days) if value < day), None)
        def add(dataset, value):
            try:
                job = queue_dataset(db, identity[0], dataset, value, batch_id=batch_id)
                # A previously complete or pending job is shared by batches;
                # the receipt's explicit ids preserve its immutable context.
                jobs.append(str(job.id))
            except APIError as error:
                skipped.append(dict(dataset=dataset, day=str(value), reason=error.code))
        for value in reversed(window):
            for dataset in ('themes', 'industry_themes'):
                add(dataset, value)
            if body.lookback > 5:
                for dataset in ('theme_funds', 'industry_funds'):
                    add(dataset, value)
        if previous:
            for dataset in ('theme_funds', 'industry_funds'):
                add(dataset, previous)
        for family in sorted(public):
            try:
                job = enqueue_collection(db, family_id(family), request_id=request.state.request_id)
                jobs.append(str(job.id))
            except APIError as error:
                skipped.append(dict(dataset=family, day=None, reason=error.code))
        # A completed receipt anchors dependency jobs without introducing a
        # new worker kind or requiring a database migration.
        from app.models import Job
        stamp = now(db)
        receipt = Job(owner_id=identity[0].id, kind='review.prepare', state='succeeded',
            payload={'review_batch_id': str(batch_id), 'direction_receipt': True, 'calendar_jobs': []},
            result_ref={'collection_job_ids': sorted(set(jobs)), 'unavailable_sources': skipped}, completed_at=stamp,
            error_code='direction_sources_partial' if skipped else None)
        db.add(receipt)
        db.flush()
        db.add(AuditEvent(actor_id=identity[0].id, action='direction.collection.request',
            request_id=request.state.request_id, resource_type='job', resource_id=receipt.id,
            changed_field_names=['direction_rankings', 'requested_range']))
        db.flush()
        return dict(batch_id=str(batch_id), state='queued', jobs=sorted(set(jobs)), skipped=skipped), 'partial', ['已安排板块日表和当前公开榜单；历史人气只读取已有归档，不补造过去快照。']
    return write_once(request, identity[0], key, body, queue, status=202)


@router.get('/api/v1/direction-data/collections/{batch_id}', response_model=Envelope[dict],
            responses=ERRORS, operation_id='getDirectionCollection')
def read_collection(batch_id: uuid.UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    return envelope(request, batch_status(db, identity[0].id, batch_id), 'partial')
