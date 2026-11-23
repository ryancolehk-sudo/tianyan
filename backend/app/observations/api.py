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
from app.observations import schemas as s, service
from app.observations.models import AuctionObservation
from app.schemas import Envelope, Page

router = APIRouter()


@router.post('/api/v1/auction-observations', response_model=Envelope[s.Observation], status_code=201,
             responses=WRITE_ERRORS, operation_id='createAuctionObservation')
def create_observation(body: s.ObservationInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin', 'researcher')
    return write_once(request, identity[0], key, body, lambda db: service.create(db, identity[0], body, request.state.request_id))


@router.get('/api/v1/auction-observations', response_model=Envelope[Page[s.Observation]], responses={**READ_ERRORS, 409: WRITE_ERRORS[409]}, operation_id='listAuctionObservations')
def list_observations(request: Request, plan_revision_id: UUID, scenario_id: UUID | None = None,
                      as_of_time: AwareDatetime | None = None, cursor: str | None = Query(default=None, max_length=512),
                      limit: int = Query(default=100, ge=1, le=500), identity=Depends(principal), db: Session = Depends(db_session)):
    service.plan_reference(db, identity[0], plan_revision_id, scenario_id)
    stmt = select(AuctionObservation).where(AuctionObservation.owner_id == identity[0].id, AuctionObservation.plan_revision_id == plan_revision_id)
    if scenario_id is not None:
        stmt = stmt.where(AuctionObservation.scenario_id == scenario_id)
    if as_of_time is not None:
        stmt = stmt.where(AuctionObservation.recorded_at <= as_of_time)
    rows, next_cursor = paged(db, stmt, AuctionObservation, identity[0].id,
        {'resource': 'auction_observations', 'plan_revision_id': plan_revision_id, 'scenario_id': scenario_id, 'as_of_time': as_of_time},
        'recorded_at', cursor, limit)
    return envelope(request, {'items': [service.dto(row) for row in rows], 'next_cursor': next_cursor}, 'complete' if rows else 'empty', as_of_time=as_of_time)


@router.get('/api/v1/auction-observations/{observation_id}', response_model=Envelope[s.Observation], responses=READ_ERRORS, operation_id='getAuctionObservation')
def get_observation(observation_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    row = db.scalar(select(AuctionObservation).where(AuctionObservation.id == observation_id, AuctionObservation.owner_id == identity[0].id))
    if row is None:
        raise APIError(404, 'not_found', '观察记录不存在或不可访问。')
    return envelope(request, service.dto(row), *service.quality(row))
