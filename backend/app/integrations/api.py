from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.auth import csrf, principal
from app.db import db_session
from app.evidence.api import business_key
from app.http import envelope
from app.integrations.monitor_schemas import Calls, Overview
from app.integrations.coverage_schemas import CoverageRepairInput, CoverageRepairReport, CoverageRepairStatus
from app.integrations.inspection_schemas import Inspection, InspectionProgress, InspectionRun, RepairBatch
from app.integrations.routing_schemas import (
    CapabilityMatrix,
    ConfiguredPorts,
    ConfigurePorts,
    FullMinuteRequest,
    MinuteChunkReceipt,
    MinuteJobView,
    ProviderConnect,
    RoutesPatch,
)
from app.schemas import Envelope, Error

router = APIRouter()
ERRORS = {code: {'model': Error} for code in (401, 403, 404, 422, 503)}


@router.get('/api/v1/data-center/overview', response_model=Envelope[Overview], responses=ERRORS,
            operation_id='getDataCenterOverview')
def get_overview(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    from app.integrations.monitor import overview
    return envelope(request, overview(db, identity[0]))


@router.get('/api/v1/data-center/inspection', response_model=Envelope[Inspection], responses=ERRORS,
            operation_id='getSystemInspection')
def system_inspection(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    from app.auth import require_role
    from app.integrations.inspection import inspect
    require_role(identity[0], 'admin')
    return envelope(request, inspect(db, identity[0]))


@router.get('/api/v1/data-center/inspection/progress', response_model=Envelope[InspectionProgress], responses=ERRORS,
            operation_id='getSystemInspectionProgress')
def inspection_progress(request: Request, operation_key: str | None = Query(default=None, min_length=8, max_length=128, pattern=r'^[a-zA-Z0-9._:-]+$'),
                        identity=Depends(principal), db: Session = Depends(db_session)):
    from app.auth import require_role
    from app.integrations.inspection_runs import read_progress
    require_role(identity[0], 'admin')
    return envelope(request, read_progress(db, identity[0], operation_key))


@router.post('/api/v1/data-center/inspection/runs', response_model=Envelope[InspectionRun], status_code=202,
             responses={**ERRORS, 409: {'model': Error}}, operation_id='startSystemInspectionRepair')
def start_inspection_repair(request: Request, identity=Depends(csrf), key=Depends(business_key)):
    from app.auth import require_role
    from app.evidence.common import write_once
    from app.integrations.inspection_runs import submit
    require_role(identity[0], 'admin')
    return write_once(request, identity[0], key, {}, lambda db: submit(db, identity[0]), status=202)


@router.post('/api/v1/data-center/inspection/repair', response_model=Envelope[RepairBatch],
             responses={**ERRORS, 409: {'model': Error}}, operation_id='repairSystemInspection')
def repair_system_inspection(request: Request, identity=Depends(csrf), key=Depends(business_key)):
    from app.auth import require_role
    from app.evidence.common import write_once
    from app.integrations.inspection_repair import repair
    require_role(identity[0], 'admin')
    return write_once(request, identity[0], key, {}, lambda db: repair(db, identity[0]), status=200)


@router.get('/api/v1/data-center/coverage-repair', response_model=Envelope[CoverageRepairStatus],
            responses=ERRORS, operation_id='getFeatureCoverageRepair')
def feature_repair_status(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    from app.auth import require_role
    from app.integrations.coverage_repair import latest
    require_role(identity[0], 'admin')
    return envelope(request, CoverageRepairStatus(last_repair=latest(db, identity[0])))


@router.post('/api/v1/data-center/coverage-repair', response_model=Envelope[CoverageRepairReport],
             responses={**ERRORS, 409: {'model': Error}}, operation_id='repairFeatureCoverage')
def repair_feature_coverage(body: CoverageRepairInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    from app.auth import require_role
    from app.evidence.common import write_once
    from app.integrations.coverage_repair import submit
    require_role(identity[0], 'admin')
    return write_once(request, identity[0], key, body,
        lambda db: (submit(db, identity[0], body, request.state.request_id), 'complete', []), status=200)


@router.get('/api/v1/data-center/calls', response_model=Envelope[Calls], responses=ERRORS,
            operation_id='listDataCenterCalls')
def get_calls(request: Request, kind: Literal['ai', 'data'] | None = None,
              connection_id: UUID | None = None, errors_only: bool = False,
              from_date: date | None = None, to_date: date | None = None,
              cursor: str | None = Query(default=None, max_length=1024),
              limit: int = Query(default=50, ge=1, le=100),
              identity=Depends(principal), db: Session = Depends(db_session)):
    from app.integrations.monitor import calls
    result = calls(db, identity[0], kind=kind, connection_id=connection_id,
                   errors_only=errors_only, from_date=from_date, to_date=to_date, cursor=cursor, limit=limit)
    return envelope(request, result, 'complete' if result.items else 'empty')


@router.get('/api/v1/data-center/capabilities', response_model=Envelope[CapabilityMatrix], responses=ERRORS,
            operation_id='getDataCapabilityMatrix')
def get_capabilities(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    from app.integrations.routing import matrix
    return envelope(request, matrix(db))


@router.patch('/api/v1/data-center/routes', response_model=Envelope[CapabilityMatrix],
              responses={**ERRORS, 409: {'model': Error}}, operation_id='updateDataCapabilityRoutes')
def update_routes(body: RoutesPatch, request: Request,
                  identity=Depends(csrf), key=Depends(business_key)):
    from app.auth import require_role
    from app.evidence.common import write_once
    from app.integrations.routing import apply_routes
    require_role(identity[0], 'admin')
    return write_once(request, identity[0], key, body,
        lambda db: apply_routes(db, identity[0], body, request.state.request_id), status=200)


@router.post('/api/v1/data-center/routes/configure', response_model=Envelope[ConfiguredPorts],
             responses={**ERRORS, 409: {'model': Error}}, operation_id='configureDataPorts')
def configure_data_ports(body: ConfigurePorts, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    from app.auth import require_role
    from app.evidence.common import write_once
    from app.integrations.routing import configure_ports
    require_role(identity[0], 'admin')
    return write_once(request, identity[0], key, body,
        lambda db: configure_ports(db, identity[0], body, request.state.request_id), status=200)


@router.post('/api/v1/data-center/providers/{provider_id}/connect', response_model=Envelope[CapabilityMatrix],
             responses={**ERRORS, 409: {'model': Error}}, operation_id='connectDataProvider')
def connect_data_provider(provider_id: str, body: ProviderConnect, request: Request,
                          identity=Depends(csrf), key=Depends(business_key)):
    from app.ai.service import safe_body
    from app.auth import require_role
    from app.evidence.common import write_once
    from app.integrations.routing import connect_provider
    require_role(identity[0], 'admin')
    return write_once(request, identity[0], key, safe_body(body),
        lambda db: connect_provider(db, identity[0], provider_id, body, request.state.request_id), status=200)


@router.post('/api/v1/data-center/full-minute', response_model=Envelope[MinuteChunkReceipt],
             responses={**ERRORS, 409: {'model': Error}}, status_code=202, operation_id='queueFullMinuteChunk')
def full_minute_chunk(body: FullMinuteRequest, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    from app.auth import APIError, require_role
    from app.evidence.common import write_once
    from app.integrations.routing import route_settings, selected_providers
    from app.market.models import SourceSettings
    from app.market.service import source_id
    from app.market.tdx_full_minute import enqueue_full_minute
    require_role(identity[0], 'admin')
    def queue(db):
        config = db.get(SourceSettings, source_id('tdx_full_minute'))
        routes = route_settings(db)
        choice = (routes.choices if routes else {}).get('full_minute', {})
        if not config or 'tdx_data' not in selected_providers(choice) or not choice.get('allow_points'):
            raise APIError(422, 'full_minute_not_selected', '请先检测并选择通达信全量分钟能力，明确启用积分。')
        if not body.allow_points:
            raise APIError(422, 'points_confirmation_required', '每段最多20只，请明确允许本次积分请求。')
        mode = 'full_market' if body.full_market else 'selected'
        if body.cursor and config.config.get('collection_mode', 'selected') != mode:
            raise APIError(409, 'configuration_changed', '续接任务的范围已变化，请重新开始。')
        config.config = {**config.config, 'collection_mode': mode, 'points_enabled': True}
        db.flush()
        job = enqueue_full_minute(db, config.source_id, day=body.day, cursor=body.cursor, request_id=request.state.request_id)
        context = job.payload['full_minute_context']
        return dict(job_id=str(job.id), state=job.state, request_limit=context['request_limit'], scope_count=context['total']), 'partial', ['已排队一个有界分段；下一段需要手动继续。']
    return write_once(request, identity[0], key, body, queue, status=202)


@router.get('/api/v1/data-center/minute-jobs/{job_id}', response_model=Envelope[MinuteJobView], responses=ERRORS,
            operation_id='getFullMinuteChunk')
def minute_job(job_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    from app.auth import APIError
    from app.models import Job
    job = db.get(Job, job_id)
    if (not job or job.kind != 'source.collect' or 'full_minute_context' not in job.payload
            or job.owner_id != identity[0].id and identity[0].role != 'admin'):
        raise APIError(404, 'not_found', '分钟补齐任务不存在或不可访问。')
    return envelope(request, dict(job_id=str(job.id), state=job.state, error_code=job.error_code, progress=job.result_ref))
