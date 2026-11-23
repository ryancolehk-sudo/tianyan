"""On-demand research gateway. It never schedules collection or writes market history."""
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.ai import service, vault
from app.ai.models import Connection
from app.auth import APIError, csrf, principal, require_instance, require_role
from app.db import engine
from app.evidence.api import business_key
from app.evidence.common import canonical, content_hash, lock
from app.evidence.models import IdempotencyKey
from app.http import envelope
from app.models import AuditEvent
from app.schemas import DTO, Envelope
from app.integrations.ifind_client import Client, DOMAINS, ENDPOINT, IfindFailure

TEMPLATE = 'ifind_mcp'
DOMAIN = Literal['stock', 'fund', 'edb', 'news', 'bond', 'global_stock', 'index', 'future']
router = APIRouter()
LIMITATIONS = ['供应商原始查询答复，未转换为行情或板块资金归档。',
               '源日期与单位以答复为准；接收时间不是行情时间，不用于历史时点回放。',
               '仅手动查询；本机每日最多8次取数，不自动重试、购买或切换付费服务。试用余额以供应商账户为准。']


class IfindRequest(DTO):
    domain: DOMAIN
    tool: str | None = Field(default=None, max_length=80)
    arguments: dict = Field(default_factory=dict)
    expected_revision: int = Field(ge=1)

    @model_validator(mode='after')
    def safe(self):
        if self.tool is not None and self.tool not in DOMAINS[self.domain][2]:
            raise ValueError('不支持的iFinD工具')
        if len(canonical(self.arguments)) > 8000 or (not self.tool and self.arguments):
            raise ValueError('参数过长或未指定工具')
        def valid(value, depth=0):
            if depth > 4:
                return False
            if isinstance(value, dict):
                return len(value) <= 30 and all(isinstance(k, str) and k not in {'__proto__', 'constructor', 'prototype'} and valid(v, depth+1) for k, v in value.items())
            if isinstance(value, list):
                return len(value) <= 30 and all(valid(v, depth+1) for v in value)
            return isinstance(value, (str, int, float, bool)) or value is None
        if not valid(self.arguments):
            raise ValueError('参数结构不支持')
        return self


class IfindResult(DTO):
    status: Literal['running', 'success', 'error', 'unknown']
    connection_id: UUID
    domain: DOMAIN
    tool: str | None = None
    revision: int
    received_at: datetime | None = None
    source_time: None = None
    historical_eligible: Literal[False] = False
    text: str | None = None
    tools: list[dict] = Field(default_factory=list)
    request_count: int = 0
    error_code: str | None = None
    limitations: list[str] = Field(default_factory=lambda: list(LIMITATIONS))


def template():
    return dict(id=TEMPLATE, name='同花顺 iFinD 官方 MCP · 手动研究', kind='market', endpoint=ENDPOINT,
                credential_required=True, endpoints=[ENDPOINT], source_families=[])


def validate(body):
    if body.kind != 'market' or body.endpoint != ENDPOINT or body.source_ids or body.protocol or body.model:
        raise APIError(422, 'ifind_configuration_invalid', 'iFinD使用固定官方地址和独立研究入口，不绑定其他供应商采集能力。')
    return body


def current(db, connection_id, owner_id, *, revision=None, enabled=True):
    row = service.get_connection(db, connection_id, owner_id=owner_id)
    if row.template != TEMPLATE or row.endpoint != ENDPOINT or row.kind != 'market':
        raise APIError(422, 'ifind_connection_required', '请选择官方iFinD研究接入。')
    if revision is not None and revision != row.revision:
        raise APIError(409, 'configuration_changed', '配置已变化，请重新加载。')
    if enabled and (not row.enabled or row.tested_revision != row.revision):
        raise APIError(409, 'ifind_not_ready', '请先保存凭据，检测连接并启用。')
    return row


def probe(db, supplied):
    from app.ai.probes import existing_input
    row = current(db, supplied.id, None, revision=supplied.expected_revision, enabled=False)
    if supplied.api_key or service.safe_body(supplied) != service.safe_body(existing_input(row)):
        raise APIError(409, 'configuration_unsaved', '请先保存当前配置再检测。')
    token = vault.read_secret(row.secret_ref)
    if not token:
        raise APIError(422, 'api_key_required', '请先保存iFinD凭据。')
    client = Client(token, 'index', row.options.get('timeout_seconds', 60))
    try:
        tools = client.tools()
        if not tools:
            raise IfindFailure('ifind_tool_unavailable')
        status, code = 'success', None
    except IfindFailure as exc:
        status, code = 'error', exc.code
    row.tested_revision = row.revision if status == 'success' else None
    return dict(status=status, revision=row.revision, request_count=client.request_count, error_code=code,
                message='指数板块工具目录连接通过；取数权益、其他领域和资金分钟覆盖需分别核验。' if status == 'success' else 'iFinD连接未通过；未开启采集或付费。')


def run(request, user, connection_id, body, key):
    identity = (user.id, 'POST', request.url.path, key)
    fingerprint = content_hash(body)
    clock = datetime.now(UTC)
    action = 'ifind.query' if body.tool else 'ifind.catalog'
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        require_instance(db)
        lock(db, 'ifind:budget')
        row = current(db, connection_id, user.id, revision=body.expected_revision)
        prior = db.get(IdempotencyKey, identity)
        if prior and prior.expires_at > clock:
            if prior.payload_hash != fingerprint:
                raise APIError(409, 'idempotency_conflict', '操作编号已用于其他查询。')
            return JSONResponse(prior.response_json, status_code=prior.status_code, headers={'Idempotency-Replayed': 'true'})
        if prior:
            db.delete(prior)
            db.flush()
        # Global allowance, across users/connections/processes; uncertain calls count too.
        midnight = clock.astimezone(ZoneInfo('Asia/Shanghai')).replace(hour=0, minute=0, second=0, microsecond=0)
        used = db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == action, AuditEvent.created_at >= midnight))
        if used >= (8 if body.tool else 32):
            raise APIError(429, 'ifind_local_daily_limit', '本机今日iFinD调用上限已到；不会自动追加额度。')
        raw_key = service.connection_credential(db, row, user.id)
        if not raw_key:
            raise APIError(422, 'api_key_required', 'iFinD凭据缺失，请重新配置。')
        client = Client(raw_key, body.domain, row.options.get('timeout_seconds', 60))
        result = IfindResult(status='running', connection_id=row.id, domain=body.domain, tool=body.tool, revision=row.revision)
        pending = jsonable_encoder(envelope(request, result, 'partial', LIMITATIONS))
        db.add(IdempotencyKey(owner_id=user.id, method='POST', route=request.url.path, key=key, payload_hash=fingerprint,
                              response_json=pending, status_code=202, expires_at=clock + timedelta(hours=24)))
        service.audit(db, user, action, row.id, [body.domain, body.tool or 'tools/list'], request.state.request_id)
    # The reservation is committed before IO. Process loss never silently replays a paid/quota call.
    with Session(engine()) as db, db.begin():
        # One request at a time across all backend processes; no scheduler or retry loop.
        if not db.scalar(text('SELECT pg_try_advisory_xact_lock(804368692414571039)')):
            result.status, result.error_code = 'error', 'ifind_busy'
        else:
            try:
                if body.tool:
                    result.text = client.call(body.tool, body.arguments)
                else:
                    result.tools = client.tools()
                result.status = 'success'
            except IfindFailure as exc:
                result.status = 'unknown' if exc.code in {'ifind_outcome_unknown', 'ifind_timeout'} and client.call_sent else 'error'
                result.error_code = exc.code
            result.request_count = client.request_count
        result.received_at = datetime.now(UTC)
        # Revocation/reconfiguration during IO must not expose stale results or update health.
        live = db.get(Connection, connection_id, populate_existing=True)
        if not live or live.archived or not live.enabled or live.revision != body.expected_revision:
            result.status, result.error_code, result.text, result.tools = 'error', 'configuration_changed', None, []
        response = jsonable_encoder(envelope(request, result, 'partial' if result.status == 'success' else 'unavailable', LIMITATIONS))
        stored = db.get(IdempotencyKey, identity)
        stored.response_json, stored.status_code = response, 200
        service.audit(db, user, 'ifind.' + result.status, connection_id, [body.domain, result.error_code or 'source_response'], request.state.request_id)
    return JSONResponse(response)


@router.get('/api/v1/integrations/ifind/domains', response_model=Envelope[list[dict]], operation_id='listIfindDomains')
def domains(request: Request, identity=Depends(principal)):
    return envelope(request, [dict(id=key, name=value[0], tools=list(value[2])) for key, value in DOMAINS.items()])


@router.post('/api/v1/integrations/{connection_id}/ifind/query', response_model=Envelope[IfindResult], operation_id='queryIfind')
def query(connection_id: UUID, body: IfindRequest, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin')
    return run(request, identity[0], connection_id, body, key)
