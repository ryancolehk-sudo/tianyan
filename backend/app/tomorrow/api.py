from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime
from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.orm import Session

from app.auth import APIError, csrf, principal, require_role
from app.db import db_session
from app.evidence.api import WRITE_ERRORS, business_key
from app.evidence.common import now, write_once
from app.http import envelope
from app.schemas import Envelope
from app.tomorrow import schemas as s
from app.tomorrow import service
from app.tomorrow.models import PlanRun, PlanValidation

router = APIRouter(prefix='/api/v1/tomorrow', tags=['tomorrow'])


@router.get('/evaluation', response_model=Envelope[dict], operation_id='getTomorrowShadowEvaluation')
def get_evaluation(request: Request, limit: int = Query(default=20, ge=1, le=60),
                   as_of_time: AwareDatetime | None = None,
                   identity=Depends(principal), db: Session = Depends(db_session)):
    from app.tomorrow.evaluation import shadow_evaluation
    result = shadow_evaluation(db, identity[0].id, as_of_time, limit)
    return envelope(request, result, 'partial', result['limitations'])


@router.get('/settings', response_model=Envelope[dict], operation_id='getTomorrowSettings')
def get_settings(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    return envelope(request, service.settings_view(db, identity[0].id))


@router.patch('/settings', response_model=Envelope[dict], responses=WRITE_ERRORS, operation_id='updateTomorrowSettings')
def patch_settings(body: s.SettingsPatch, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin', 'researcher')
    return write_once(request, identity[0], key, body,
        lambda db: service.update_settings(db, identity[0], body, request.state.request_id), status=200)


@router.post('/runs', response_model=Envelope[s.RunView], responses=WRITE_ERRORS, status_code=201, operation_id='createTomorrowRun')
def create_run(body: s.RunInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin', 'researcher')
    return write_once(request, identity[0], key, body,
        lambda db: service.queue_run(db, identity[0], body, request.state.request_id))


@router.get('/runs', response_model=Envelope[dict], operation_id='listTomorrowRuns')
def list_runs(request: Request, source_trade_day: date | None = None,
              limit: int = Query(default=30, ge=1, le=100), identity=Depends(principal), db: Session = Depends(db_session)):
    query = select(PlanRun).where(PlanRun.owner_id == identity[0].id)
    if source_trade_day:
        query = query.where(PlanRun.source_trade_day == source_trade_day)
    rows = db.scalars(query.order_by(PlanRun.created_at.desc(), PlanRun.id.desc()).limit(limit)).all()
    return envelope(request, {'items': [service.run_view(db, row) for row in rows], 'next_cursor': None})


@router.get('/runs/{run_id}', response_model=Envelope[s.RunView], operation_id='getTomorrowRun')
def get_run(run_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    return envelope(request, service.run_view(db, service.owned_run(db, identity[0].id, run_id)))


@router.get('/runs/{run_id}/objects', response_model=Envelope[dict], operation_id='getTomorrowDossierObjects')
def get_objects(run_id: UUID, request: Request, kind: Literal['stock', 'sector', 'event'] = 'stock',
                offset: int = Query(default=0, ge=0, le=100000), limit: int = Query(default=10, ge=1, le=30),
                identity=Depends(principal), db: Session = Depends(db_session)):
    run = service.owned_run(db, identity[0].id, run_id)
    field = {'stock': 'stocks', 'sector': 'sectors', 'event': 'events'}[kind]
    result = db.execute(select(func.jsonb_array_length(PlanRun.bundle[field]),
        func.jsonb_path_query_array(PlanRun.bundle[field], cast(f'$[{offset} to {offset + limit - 1}]', JSONPATH)))
        .where(PlanRun.id == run_id)).one()
    items = result[1] or []
    refs = {ref for item in items for ref in item.get('evidence_refs') or []}
    if set(service.permitted(db, run.owner_id, refs, run.as_of_time)) != refs:
        raise APIError(409, 'input_permission_changed', '本页底稿所需来源当前不可用，未显示不完整依据。')
    return envelope(request, dict(items=items, total=result[0] or 0, offset=offset, limit=limit,
        kind=kind, input_hash=run.input_hash, as_of_time=run.as_of_time.isoformat()), 'partial', run.limitations)


@router.get('/reports/{report_id}', response_model=Envelope[s.PlanReportV1], operation_id='getTomorrowReport')
def get_report(report_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    view = service.report_view(db, identity[0].id, report_id)
    return envelope(request, view, 'partial' if view['limitations'] else 'complete', view['limitations'])


@router.post('/reports/{report_id}/revisions', response_model=Envelope[s.PlanReportV1], responses=WRITE_ERRORS,
             status_code=201, operation_id='reviseTomorrowReport')
def revise_report(report_id: UUID, body: s.RevisionInput, request: Request,
                  identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin', 'researcher')
    return write_once(request, identity[0], key, body,
        lambda db: service.revise(db, identity[0], report_id, body, request.state.request_id))


@router.post('/reports/{report_id}/confirm', response_model=Envelope[s.PlanReportV1], responses=WRITE_ERRORS,
             operation_id='confirmTomorrowReport')
def confirm_report(report_id: UUID, body: s.ConfirmInput, request: Request,
                   identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin', 'researcher')
    return write_once(request, identity[0], key, body,
        lambda db: service.confirm(db, identity[0], report_id, body, request.state.request_id), status=200)


@router.get('/reports/{report_id}/validations', response_model=Envelope[dict], operation_id='listTomorrowValidations')
def list_validations(report_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    service.report_view(db, identity[0].id, report_id)
    rows = db.scalars(select(PlanValidation).where(PlanValidation.report_id == report_id,
        PlanValidation.owner_id == identity[0].id).order_by(PlanValidation.created_at, PlanValidation.id)).all()
    return envelope(request, {'items': [service.validation_view(row) for row in rows]})


@router.get('/reports/{report_id}/validation-evidence', response_model=Envelope[dict], operation_id='getTomorrowValidationEvidence')
def get_validation_evidence(report_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    return envelope(request, service.validation_evidence(db, identity[0].id, report_id))


@router.post('/reports/{report_id}/validations', response_model=Envelope[dict], responses=WRITE_ERRORS,
             status_code=201, operation_id='appendTomorrowValidation')
def validate_report(report_id: UUID, body: s.ValidationInput, request: Request,
                    identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin', 'researcher')
    return write_once(request, identity[0], key, body,
        lambda db: service.validate(db, identity[0], report_id, body, request.state.request_id))


@router.post('/prepare', response_model=Envelope[dict], responses=WRITE_ERRORS, status_code=201,
             operation_id='prepareTomorrowEvidence')
def prepare_evidence(body: s.PrepareInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin', 'researcher')
    def create(db):
        from app.market.review_store import available_trade_days
        from app.tomorrow.prepare import queue_preparation
        cutoff = now(db)
        days = available_trade_days(db, as_of_time=cutoff)
        day = body.source_trade_day or (max(days) if days else None)
        if day is None or day > cutoff.astimezone(service.SHANGHAI).date():
            raise APIError(422, 'trading_day_unavailable', '请先选择可核验的来源交易日。')
        result = queue_preparation(db, identity[0].id, day, cutoff, body.candidates)
        service.audit(db, identity[0], 'prepare.create', request_id=request.state.request_id)
        return result, 'partial', result.get('limitations', [])
    return write_once(request, identity[0], key, body, create)
