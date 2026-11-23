import logging
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from app import schemas as s
from app.access_logging import PrivateAccessLog as _PrivateOAuthAccessLog
from app.auth import COOKIE, APIError, _dummy_hash, csrf, digest, hasher, login_allowed, principal, require_instance, require_origin, verify_password
from app.config import settings
from app.db import db_session
from app.models import AuditEvent, InstanceState, Instrument, Job, LoginSession, LoginThrottle, Provider, User, WorkerHeartbeat
from app.http import envelope
from app.market.visibility import instrument_name_sql, instrument_visible_sql
from app.version import SCHEMA_REVISION
from app.maintenance_control import request_admission

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("jiulong")


logging.getLogger("uvicorn.access").addFilter(_PrivateOAuthAccessLog())
app = FastAPI(title="天衍系统", version="0.4.0", docs_url=None, redoc_url=None, openapi_url=None,
              dependencies=[Depends(request_admission)])
AUTH_ERRORS = {401: {"model": s.Error}, 503: {"model": s.Error}}


def error(request, status, code, message, details=None):
    return JSONResponse(status_code=status, content={
        "error": {"code": code, "message": message, "details": details or []},
        "request_id": str(request.state.request_id),
    })


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = uuid.uuid4()
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        log.error("unhandled request_id=%s", request.state.request_id)
        response = error(request, 500, "internal_error", "服务暂时异常，请稍后重试。")
    response.headers["X-Request-ID"] = str(getattr(request.state, "replayed_request_id", request.state.request_id))
    # Cacheable reads explicitly opt in only after authentication and scope
    # checks. Session, credentials, writes and every error remain no-store.
    if response.status_code >= 400 or "X-Cache-Scope" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    log.info("request_id=%s method=%s status=%s elapsed_ms=%d", request.state.request_id,
             request.method, response.status_code, (time.monotonic() - start) * 1000)
    return response


@app.exception_handler(APIError)
async def api_error(request, exc):
    return error(request, exc.status, exc.code, exc.message)


@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    return error(request, 503, "database_unavailable", "数据服务暂不可用，请稍后重试。")


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    details = [{"field": ".".join(str(x) for x in e["loc"])[:200], "reason": e["type"][:200]} for e in exc.errors()[:20]]
    return error(request, 422, "invalid_request", "请求格式不正确。", details)


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return error(request, exc.status_code, "not_found" if exc.status_code == 404 else "http_error", "请求的资源或操作不可用。")


@app.get("/health/live", response_model=s.Health, operation_id="healthLive")
def live(request: Request):
    return {"status": "ok", "request_id": request.state.request_id}


@app.get("/health/ready", response_model=s.Health, responses={503: {"model": s.Health}}, operation_id="healthReady")
def ready(request: Request, db: Session = Depends(db_session)):
    try:
        version = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        heartbeat = db.scalar(select(func.max(WorkerHeartbeat.seen_at)).where(WorkerHeartbeat.worker_id.not_like('ai:%')))
        mode = db.scalar(select(InstanceState.app_mode).where(InstanceState.id == 1))
        ok = mode == settings().app_mode and version == SCHEMA_REVISION and heartbeat and heartbeat > datetime.now(UTC) - timedelta(seconds=60)
        for role in settings().required_worker_roles:
            role_seen = db.scalar(select(func.max(WorkerHeartbeat.seen_at)).where(
                WorkerHeartbeat.worker_id.startswith('market:' + role + ':')))
            ok = bool(ok and role_seen and role_seen > datetime.now(UTC) - timedelta(seconds=60))
    except SQLAlchemyError:
        ok = False
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ok" if ok else "unavailable", "request_id": str(request.state.request_id)})


@app.post("/api/v1/session", response_model=s.Envelope[s.Session], status_code=201,
          responses={**AUTH_ERRORS, 403: {"model": s.Error}, 422: {"model": s.Error}, 429: {"model": s.Error}}, operation_id="createSession")
def login(body: s.LoginInput, request: Request, response: Response, db: Session = Depends(db_session)):
    from app.system_settings.service import audit
    require_origin(request)
    require_instance(db)
    username = body.username.lower()
    login_allowed(db, username, request.client.host if request.client else "unknown")
    user = db.scalar(select(User).where(User.username == username))
    valid = verify_password(user.password_hash if user else _dummy_hash, body.password)
    if not user or not valid or not user.enabled:
        audit(db, None, 'session.login_failed', request_id=request.state.request_id, outcome='failed')
        db.commit()
        raise APIError(401, "invalid_credentials", "账号或密码不正确。")
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hasher.hash(body.password)
    old = request.cookies.get(COOKIE)
    if old:
        db.execute(update(LoginSession).where(LoginSession.token_hash == digest(old)).values(revoked_at=func.now()))
    db.execute(delete(LoginThrottle).where(LoginThrottle.bucket == digest(f"user:{username}")))
    db.execute(delete(LoginSession).where(LoginSession.expires_at < datetime.now(UTC) - timedelta(days=1)))
    token, csrf_token = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    db.add(LoginSession(user_id=user.id, token_hash=digest(token), csrf_token=csrf_token,
                        expires_at=datetime.now(UTC) + timedelta(hours=settings().session_hours)))
    audit(db, user, 'session.login', request_id=request.state.request_id)
    db.commit()
    response.set_cookie(COOKIE, token, max_age=settings().session_hours * 3600,
                        httponly=True, secure=settings().cookie_secure, samesite="strict", path="/")
    from app.read_cache import cache_scope
    from app.system_settings.service import preferences
    return envelope(request, {"user": s.User.model_validate(user), "csrf_token": csrf_token,
                              "cache_scope": cache_scope(db, user), "preferences": preferences(db, user)})


@app.get("/api/v1/session", response_model=s.Envelope[s.Session], responses=AUTH_ERRORS, operation_id="getSession")
def current_session(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    from app.read_cache import cache_scope
    from app.system_settings.service import preferences
    return envelope(request, {"user": s.User.model_validate(identity[0]), "csrf_token": identity[1].csrf_token,
                              "cache_scope": cache_scope(db, identity[0]), "preferences": preferences(db, identity[0])})


@app.delete("/api/v1/session", status_code=204, responses={**AUTH_ERRORS, 403: {"model": s.Error}}, operation_id="deleteSession")
def logout(response: Response, request: Request, identity=Depends(csrf), db: Session = Depends(db_session)):
    from app.system_settings.service import audit
    identity[1].revoked_at = datetime.now(UTC)
    audit(db, identity[0], 'session.logout', request_id=request.state.request_id)
    db.commit()
    response.delete_cookie(COOKIE, path="/", secure=settings().cookie_secure, httponly=True, samesite="strict")


@app.get("/api/v1/providers", response_model=s.Envelope[s.Page[s.Provider]], responses=AUTH_ERRORS, operation_id="listProviders")
def providers(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(Provider).order_by(Provider.kind)).all()
    return envelope(request, {"items": [s.Provider.model_validate(p) for p in rows], "next_cursor": None}, "partial", ["登记、来源启用和实际采集分开核对；连接结果见数据源状态与调用记录"])


@app.get("/api/v1/instruments", response_model=s.Envelope[s.Page[s.Instrument]], responses={**AUTH_ERRORS, 422: {"model": s.Error}}, operation_id="listInstruments")
def instruments(request: Request, q: str = Query(default="", max_length=80), limit: int = Query(default=50, ge=1, le=100),
                cursor: uuid.UUID | None = None, identity=Depends(principal), db: Session = Depends(db_session)):
    visible_name = instrument_name_sql()
    stmt = select(Instrument.id, Instrument.exchange, Instrument.symbol, visible_name.label('name'),
        Instrument.valid_from, Instrument.valid_to, Instrument.asset_type).where(
        or_(Instrument.symbol.startswith(q, autoescape=True), visible_name.contains(q, autoescape=True),
            Instrument.local_name.contains(q, autoescape=True)),
        instrument_visible_sql()).order_by(Instrument.id)
    if cursor:
        stmt = stmt.where(Instrument.id > cursor)
    rows = list(db.execute(stmt.limit(limit + 1)).mappings().all())
    next_cursor = str(rows[limit - 1]['id']) if len(rows) > limit else None
    # Distinguish an unpopulated catalogue from a valid query with zero matches.
    catalogue_missing = not rows and db.scalar(select(Instrument.id).limit(1)) is None
    quality = "partial" if rows else "unavailable" if catalogue_missing else "empty"
    return envelope(request, {"items": [s.Instrument.model_validate(v) for v in rows[:limit]], "next_cursor": next_cursor},
                    quality, ["标的基础资料尚未导入"] if catalogue_missing else ["仅查询本机已留存的标的；完整市场目录与历史有效期不由报价推定"])


@app.get("/api/v1/jobs/{job_id}", response_model=s.Envelope[s.Job], responses={**AUTH_ERRORS, 404: {"model": s.Error}, 422: {"model": s.Error}}, operation_id="getJob")
def get_job(job_id: uuid.UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    row = db.scalar(select(Job).where(Job.id == job_id, Job.owner_id == identity[0].id))
    if not row:
        raise APIError(404, "not_found", "任务不存在或不可访问。")
    if row.kind in {'review.collect', 'review.prepare', 'ai.generate', 'news.ai'} or 'full_minute_context' in row.payload:
        # Generic ResultRef links name snapshot/rule/model resources only.
        # Review counts/manifests are exposed by their owner-scoped batch API.
        values = {name: getattr(row, name) for name in s.Job.model_fields}
        return envelope(request, s.Job.model_validate({**values, 'result_ref': None}))
    return envelope(request, s.Job.model_validate(row))


from app.evidence.api import router as evidence_router  # noqa: E402
app.include_router(evidence_router)

from app.research.api import router as research_router  # noqa: E402
app.include_router(research_router)

from app.observations.api import router as observations_router  # noqa: E402
app.include_router(observations_router)

from app.validations.api import router as validations_router  # noqa: E402
app.include_router(validations_router)

from app.review_archives.api import router as review_archives_router  # noqa: E402
app.include_router(review_archives_router)

from app.market.api import router as market_router  # noqa: E402
app.include_router(market_router)
from app.market.updates import router as workbench_updates_router  # noqa: E402
app.include_router(workbench_updates_router)

from app.news_center.api import router as news_center_router  # noqa: E402
app.include_router(news_center_router)

from app.market.review_api import router as review_data_router  # noqa: E402
app.include_router(review_data_router)

from app.market.direction_api import router as direction_router  # noqa: E402
app.include_router(direction_router)

from app.ai.api import router as ai_router  # noqa: E402
app.include_router(ai_router)

from app.integrations.api import router as data_center_router  # noqa: E402
app.include_router(data_center_router)

from app.integrations.ifind import router as ifind_router  # noqa: E402
app.include_router(ifind_router)

from app.chatgpt.api import router as chatgpt_router  # noqa: E402
app.include_router(chatgpt_router)

from app.chatgpt.model_api import router as chatgpt_model_router  # noqa: E402
app.include_router(chatgpt_model_router)

from app.screening.api import router as screening_router  # noqa: E402
app.include_router(screening_router)

from app.system_settings.api import router as system_settings_router  # noqa: E402
app.include_router(system_settings_router)

from app.updates.api import router as updates_router  # noqa: E402
app.include_router(updates_router)
from app.tomorrow.api import router as tomorrow_router  # noqa: E402
app.include_router(tomorrow_router)

from app.market.recovery_api import router as recovery_router  # noqa: E402
app.include_router(recovery_router)

# The native installation serves the already-built UI from the same origin.
# Register last so no SPA fallback can shadow an API or health endpoint.
if settings().static_directory:
    from app.native_static import mount_static
    mount_static(app, settings().static_directory)

from app.market.ths_funds_api import router as ths_funds_router
app.include_router(ths_funds_router)
from app.market.kpl_strength_api import router as kpl_strength_router
app.include_router(kpl_strength_router)
