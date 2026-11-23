from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import APIError, csrf, principal, require_role
from app.db import db_session
from app.evidence.api import business_key
from app.evidence.common import write_once
from app.http import envelope
from app.schemas import Envelope, Error
from app.screening import schemas as s
from app.screening import service
from app.screening.models import ScreenEvent, ScreenRule, ScreenRun

router = APIRouter()
ERRORS = {code: {"model": Error} for code in (401, 403, 404, 409, 422, 429, 503)}


@router.get(
    "/api/v1/screening/catalogue",
    response_model=Envelope[s.Catalogue],
    responses=ERRORS,
    operation_id="getScreeningCatalogue",
)
def catalogue(request: Request, identity=Depends(principal)):
    return envelope(request, service.catalogue())


@router.get(
    "/api/v1/screening/rules",
    response_model=Envelope[s.RuleList],
    responses=ERRORS,
    operation_id="listScreeningRules",
)
def rules(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    rows = db.scalars(
        select(ScreenRule)
        .where(ScreenRule.owner_id == identity[0].id)
        .order_by(ScreenRule.created_at, ScreenRule.id)
        .limit(service.MAX_RULES)
    ).all()
    return envelope(
        request, {"items": [service.rule_dto(row) for row in rows]}, "complete" if rows else "empty"
    )


@router.post(
    "/api/v1/screening/rules",
    response_model=Envelope[s.Rule],
    responses=ERRORS,
    operation_id="saveScreeningRule",
)
def save(body: s.RuleSave, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin", "researcher")

    def create(db):
        return service.save_rule(db, identity[0], body, request.state.request_id), "complete", []

    return write_once(request, identity[0], key, body, create, status=200)


@router.post(
    "/api/v1/screening/preview",
    response_model=Envelope[s.RunResult],
    responses=ERRORS,
    operation_id="previewScreening",
)
def preview(
    body: s.PreviewInput,
    request: Request,
    identity=Depends(csrf),
    db: Session = Depends(db_session),
):
    require_role(identity[0], "admin", "researcher")
    result = service.preview(db, identity[0].id, body)
    return envelope(
        request,
        result,
        "partial" if result.counts.total else "empty",
        result.reasons,
        as_of_time=result.as_of_time,
    )


@router.post(
    "/api/v1/screening/rules/{rule_id}/runs",
    response_model=Envelope[s.RunResult],
    responses=ERRORS,
    operation_id="runScreeningRule",
)
def run(
    rule_id: UUID,
    body: s.ContextInput,
    request: Request,
    identity=Depends(csrf),
    key=Depends(business_key),
):
    require_role(identity[0], "admin", "researcher")

    def create(db):
        row = service.run_rule(db, identity[0].id, rule_id, body)
        result = service.run_result(db, row)
        return result, "partial" if result.counts.total else "empty", result.reasons

    def validate_replay(db, response):
        row = db.scalar(
            select(ScreenRun).where(
                ScreenRun.id == UUID(response["data"]["id"]), ScreenRun.owner_id == identity[0].id
            )
        )
        if row is None or not service.readable(db, row):
            raise APIError(
                403, "screen_source_permission", "来源当前已撤权，历史运行重试暂停展示。"
            )

    return write_once(
        request, identity[0], key, body, create, status=200, validate_replay=validate_replay
    )


@router.get(
    "/api/v1/screening/runs",
    response_model=Envelope[s.RunList],
    responses=ERRORS,
    operation_id="listScreeningRuns",
)
def runs(
    request: Request,
    rule_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    identity=Depends(principal),
    db: Session = Depends(db_session),
):
    stmt = select(ScreenRun).where(ScreenRun.owner_id == identity[0].id)
    if rule_id is not None:
        service.get_rule(db, identity[0].id, rule_id)
        stmt = stmt.where(ScreenRun.rule_id == rule_id)
    rows = db.scalars(
        stmt.order_by(ScreenRun.created_at.desc(), ScreenRun.id.desc()).limit(limit)
    ).all()
    items = [service.summary(row) for row in rows if service.readable(db, row)]
    return envelope(request, {"items": items}, "complete" if items else "empty")


@router.get(
    "/api/v1/screening/runs/{run_id}",
    response_model=Envelope[s.RunResult],
    responses=ERRORS,
    operation_id="getScreeningRun",
)
def result(
    run_id: UUID,
    request: Request,
    state: s.ResultState | None = None,
    offset: int = Query(default=0, ge=0, le=10000),
    limit: int = Query(default=100, ge=1, le=200),
    identity=Depends(principal),
    db: Session = Depends(db_session),
):
    row = db.scalar(
        select(ScreenRun).where(ScreenRun.id == run_id, ScreenRun.owner_id == identity[0].id)
    )
    if row is None:
        raise APIError(404, "not_found", "研究运行不存在或不可访问。")
    data = service.run_result(db, row, state=state, offset=offset, limit=limit)
    return envelope(
        request,
        data,
        "partial" if data.counts.total else "empty",
        data.reasons,
        as_of_time=data.as_of_time,
    )


@router.get(
    "/api/v1/screening/events",
    response_model=Envelope[s.EventList],
    responses=ERRORS,
    operation_id="listScreeningEvents",
)
def events(
    request: Request,
    rule_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    identity=Depends(principal),
    db: Session = Depends(db_session),
):
    stmt = (
        select(ScreenEvent, ScreenRun)
        .join(ScreenRun, ScreenRun.id == ScreenEvent.run_id)
        .where(ScreenEvent.owner_id == identity[0].id)
    )
    if rule_id is not None:
        service.get_rule(db, identity[0].id, rule_id)
        stmt = stmt.where(ScreenEvent.rule_id == rule_id)
    rows = db.execute(
        stmt.order_by(ScreenEvent.created_at.desc(), ScreenEvent.id.desc()).limit(limit)
    ).all()
    items = [s.Event.model_validate(event) for event, row in rows if service.readable(db, row)]
    return envelope(request, {"items": items}, "complete" if items else "empty")
