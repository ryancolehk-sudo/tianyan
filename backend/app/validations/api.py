from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import APIError, csrf, principal, require_role
from app.db import db_session
from app.evidence.api import READ_ERRORS, WRITE_ERRORS, business_key, paged
from app.evidence.common import write_once
from app.http import envelope
from app.observations.service import plan_reference
from app.schemas import Envelope, Page
from app.validations import schemas as s, service
from app.validations.models import ScenarioValidation

router = APIRouter()


@router.post('/api/v1/validations', response_model=Envelope[s.Validation], status_code=201, responses=WRITE_ERRORS, operation_id='createValidation')
def create_validation(body: s.ValidationInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin', 'researcher')
    return write_once(request, identity[0], key, body, lambda db: service.create(db,identity[0],body,request.state.request_id))


@router.get('/api/v1/validations', response_model=Envelope[Page[s.Validation]], responses={**READ_ERRORS,409:WRITE_ERRORS[409]}, operation_id='listValidations')
def list_validations(request: Request, plan_revision_id: UUID, scenario_id: UUID | None = None,
                     as_of_time: AwareDatetime | None = None, cursor: str | None = Query(default=None,max_length=512),
                     limit: int = Query(default=100,ge=1,le=500), identity=Depends(principal), db: Session = Depends(db_session)):
    plan_reference(db,identity[0],plan_revision_id,scenario_id)
    stmt=select(ScenarioValidation).where(ScenarioValidation.owner_id==identity[0].id,ScenarioValidation.plan_revision_id==plan_revision_id)
    if scenario_id is not None: stmt=stmt.where(ScenarioValidation.scenario_id==scenario_id)
    if as_of_time is not None: stmt=stmt.where(ScenarioValidation.recorded_at<=as_of_time)
    rows,next_cursor=paged(db,stmt,ScenarioValidation,identity[0].id,
        {'resource':'validations','plan_revision_id':plan_revision_id,'scenario_id':scenario_id,'as_of_time':as_of_time},'recorded_at',cursor,limit)
    # At a historical cutoff, future corrections must not leak through metadata.
    items=[]
    for row in rows:
        item=service.dto(db,row)
        if as_of_time is not None and item.superseded_by_id is not None:
            successor=db.get(ScenarioValidation,item.superseded_by_id)
            if successor.recorded_at>as_of_time: item.superseded_by_id=None
        items.append(item)
    return envelope(request,{'items':items,'next_cursor':next_cursor},'complete' if rows else 'empty',as_of_time=as_of_time)


@router.get('/api/v1/validations/{validation_id}', response_model=Envelope[s.Validation], responses=READ_ERRORS, operation_id='getValidation')
def get_validation(validation_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    row=db.scalar(select(ScenarioValidation).where(ScenarioValidation.id==validation_id,ScenarioValidation.owner_id==identity[0].id))
    if row is None: raise APIError(404,'not_found','验证记录不存在或不可访问。')
    return envelope(request,service.dto(db,row),*service.quality(row))
