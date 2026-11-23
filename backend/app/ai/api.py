from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import AwareDatetime
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.ai import probes, service, vault
from app.ai import schemas as s
from app.ai.models import AiRun, Connection
from app.auth import APIError, csrf, principal, require_role
from app.db import db_session
from app.evidence.api import business_key
from app.evidence.common import write_once
from app.http import envelope
from app.schemas import Envelope, Error, Page

router = APIRouter()
READ_ERRORS = {401: {"model": Error}, 404: {"model": Error}, 422: {"model": Error}, 503: {"model": Error}}
WRITE_ERRORS = {**READ_ERRORS, 403: {"model": Error}, 409: {"model": Error}}


@router.get("/api/v1/integrations", response_model=Envelope[s.ConnectionIndex], responses=READ_ERRORS, operation_id="listIntegrations")
def integrations(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    from app.model_connections import virtual_connections as legacy_connections
    stored = list(db.scalars(select(Connection).where(Connection.archived.is_(False),
                          or_(Connection.owner_id.is_(None), Connection.owner_id == identity[0].id))
                          .order_by(Connection.created_at, Connection.id)))
    existing = {str(row.id) for row in db.scalars(select(Connection))}
    items = [service.connection_view(db, row) for row in stored]
    items += [{key: value for key, value in item.items() if key != "known_env_ref"}
              for item in service.data_module().virtual_connections(db) + legacy_connections(db) if str(item["id"]) not in existing]
    templates = [{"id": "custom_model", "name": "自定义模型服务", "kind": "model", "endpoint": "https://api.openai.com/v1", "credential_required": True}]
    templates += service.data_module().templates(db)
    return envelope(request, {"items": items, "templates": templates, "credential_store_ready": vault.ready()})


@router.post("/api/v1/integrations", response_model=Envelope[s.ConnectionView], responses=WRITE_ERRORS, status_code=201, operation_id="createIntegration")
def create_integration(body: s.ConnectionInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin")
    return write_once(request, identity[0], key, service.safe_body(body),
                      lambda db: service.store_connection(db, identity[0], body, request_id=request.state.request_id))


@router.patch("/api/v1/integrations/{connection_id}", response_model=Envelope[s.ConnectionView], responses=WRITE_ERRORS, operation_id="updateIntegration")
def update_integration(connection_id: UUID, body: s.ConnectionInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin")
    return write_once(request, identity[0], key, service.safe_body(body),
                      lambda db: service.store_connection(db, identity[0], body, connection_id, request.state.request_id), status=200)


@router.delete("/api/v1/integrations/{connection_id}", status_code=204, responses=WRITE_ERRORS, operation_id="deleteIntegration")
def delete_integration(connection_id: UUID, request: Request, identity=Depends(csrf), db: Session = Depends(db_session)):
    require_role(identity[0], "admin")
    service.archive_connection(db, identity[0], connection_id, request.state.request_id)
    db.commit()
    return Response(status_code=204)


@router.post("/api/v1/integrations/probe", response_model=Envelope[s.ProbeResult], responses=WRITE_ERRORS, operation_id="probeIntegration")
def probe_integration(body: s.ConnectionInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin")
    if body.kind == "model":
        return probes.model_probe(request, identity[0], body, key)
    return write_once(request, identity[0], key, service.safe_body(body), lambda db: probes.data_probe(db, identity[0], body), status=200)


@router.post("/api/v1/integrations/discover-models", response_model=Envelope[s.ProbeResult], responses=WRITE_ERRORS, operation_id="discoverModels")
def discover_models(body: s.ConnectionInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin")
    if body.kind != "model":
        raise APIError(422, "model_required", "仅模型服务支持模型列表获取。")
    return probes.model_probe(request, identity[0], body, key, discovery=True)


@router.get("/api/v1/ai/settings", response_model=Envelope[s.SettingsView], responses=READ_ERRORS, operation_id="getAiSettings")
def ai_settings(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    return envelope(request, service.settings_view(db, identity[0].id))


@router.patch("/api/v1/ai/settings", response_model=Envelope[s.SettingsView], responses=WRITE_ERRORS, operation_id="updateAiSettings")
def update_ai_settings(body: s.SettingsPatch, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin")
    return write_once(request, identity[0], key, body, lambda db: service.update_settings(db, identity[0], body, request.state.request_id), status=200)


@router.patch("/api/v1/ai/bindings/{feature}", response_model=Envelope[s.SettingsView], responses=WRITE_ERRORS, operation_id="updateAiBinding")
def update_ai_binding(feature: s.Feature, body: s.BindingPatch, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin")
    return write_once(request, identity[0], key, body, lambda db: service.update_binding(db, identity[0], feature, body, request.state.request_id), status=200)


@router.post("/api/v1/ai/runs", response_model=Envelope[s.RunView], responses=WRITE_ERRORS, status_code=202, operation_id="createAiRun")
def create_ai_run(body: s.RunInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    from app.ai.runs import queue_run
    require_role(identity[0], "admin", "researcher")
    return write_once(request, identity[0], key, body, lambda db: queue_run(db, identity[0], body, request.state.request_id), status=202)


@router.get("/api/v1/ai/runs", response_model=Envelope[Page[s.RunView]], responses=READ_ERRORS, operation_id="listAiRuns")
def list_ai_runs(request: Request, feature: s.Feature | None = None, trade_day: date | None = None, news_id: UUID | None = None,
                 as_of_time: AwareDatetime | None = None,
                 limit: int = Query(default=30, ge=1, le=100), identity=Depends(principal), db: Session = Depends(db_session)):
    stmt = select(AiRun).where(AiRun.owner_id == identity[0].id)
    if feature:
        stmt = stmt.where(AiRun.feature == feature)
    if trade_day:
        stmt = stmt.where(AiRun.trade_day == trade_day)
    if news_id:
        stmt = stmt.where(AiRun.news_id == news_id)
    if as_of_time:
        stmt = stmt.where(AiRun.as_of_time == as_of_time)
    rows = db.scalars(stmt.order_by(AiRun.created_at.desc(), AiRun.id.desc()).limit(limit)).all()
    return envelope(request, {"items": [s.RunView.model_validate(row) for row in rows], "next_cursor": None})


@router.get("/api/v1/ai/runs/{run_id}", response_model=Envelope[s.RunView], responses=READ_ERRORS, operation_id="getAiRun")
def get_ai_run(run_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    row = db.scalar(select(AiRun).where(AiRun.id == run_id, AiRun.owner_id == identity[0].id))
    if row is None:
        raise APIError(404, "not_found", "生成任务不存在或不可访问。")
    return envelope(request, s.RunView.model_validate(row))
