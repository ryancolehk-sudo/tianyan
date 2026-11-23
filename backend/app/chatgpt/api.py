from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import (
    COOKIE,
    APIError,
    csrf,
    login_allowed,
    principal,
    require_instance,
    require_origin,
)
from app.chatgpt import schemas as s
from app.chatgpt import service
from app.chatgpt.models import ChatGPTAccount
from app.config import settings
from app.db import db_session
from app.http import envelope
from app.schemas import Envelope, Error

router = APIRouter()
READ_ERRORS = {401: {"model": Error}, 503: {"model": Error}}
WRITE_ERRORS = {**READ_ERRORS, 400: {"model": Error}, 403: {"model": Error}, 404: {"model": Error}, 409: {"model": Error}, 422: {"model": Error}, 429: {"model": Error}}


def optional_identity(request, db):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        return principal(request, token, db)
    except APIError as error:
        if error.status == 401:
            return None
        raise


@router.get("/api/v1/chatgpt/status", response_model=Envelope[s.ChatGPTStatus], responses={503: {"model": Error}}, operation_id="chatgptStatus")
def status(request: Request, db: Session = Depends(db_session)):
    require_instance(db)
    identity = optional_identity(request, db)
    available, reason = service.availability()
    rows = db.scalars(select(ChatGPTAccount).where(ChatGPTAccount.owner_id == identity[0].id)
        .order_by(ChatGPTAccount.created_at, ChatGPTAccount.id)).all() if identity else []
    return envelope(request, s.ChatGPTStatus(available=available, reason=reason, authenticated=identity is not None,
        accounts=[service.account_view(db, row) for row in rows]))


@router.post("/api/v1/chatgpt/login", response_model=Envelope[s.ChatGPTLoginResult], responses=WRITE_ERRORS, operation_id="chatgptLogin")
def login(body: s.ChatGPTLoginInput, request: Request, response: Response, x_csrf_token: str | None = Header(default=None), db: Session = Depends(db_session)):
    require_origin(request)
    require_instance(db)
    identity = optional_identity(request, db)
    if body.mode == "connect":
        if identity is None:
            raise APIError(401, "unauthenticated", "请登录原有账号后绑定 ChatGPT。")
        csrf(request, x_csrf_token, identity)
    login_allowed(db, "chatgpt:" + (request.client.host if request.client else "unknown"), request.client.host if request.client else "unknown")
    if body.new_account and body.account_id is not None:
        raise APIError(400, "chatgpt_account_selection_invalid", "新增账号与已保存账号的重新授权不能同时选择。")
    url, browser = service.begin_authorization(db, mode=body.mode, identity=identity, account_id=body.account_id,
        returning_client_id=request.cookies.get(service.registration_cookie_name()) if not body.new_account else None)
    response.set_cookie(service.browser_cookie_name(), browser, max_age=service.ATTEMPT_SECONDS,
        httponly=True, secure=settings().cookie_secure, samesite="lax", path=service.CALLBACK_PATH)
    return envelope(request, s.ChatGPTLoginResult(authorization_url=url))


@router.get("/api/v1/chatgpt/callback", response_class=RedirectResponse, status_code=303, operation_id="chatgptCallback")
def callback(request: Request, db: Session = Depends(db_session)):
    # Query values are never copied into errors, HTML, log records or the final redirect.
    result = "chatgpt_authorization_failed"
    response = RedirectResponse(settings().public_origin + "/data?view=connections&chatgpt=" + result, status_code=303)
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    try:
        require_instance(db)
        keys = ("state", "code", "client_id", "error")
        if any(len(request.query_params.getlist(key)) > 1 for key in keys):
            raise APIError(400, "chatgpt_state_invalid", "登录验证已失效，请重新开始。")
        owner, account, mode = service.consume_authorization(db, state=request.query_params.get("state"),
            browser=request.cookies.get(service.browser_cookie_name()), client_id=request.query_params.get("client_id"),
            code=request.query_params.get("code"), error=request.query_params.get("error"))
        if mode == "login":
            service.issue_session(db, owner, request, response)
            result = "signed_in"
        else:
            result = "connected"
        db.commit()
        db.info.pop("chatgpt_uncommitted_secrets", None)
        response.set_cookie(service.registration_cookie_name(), account.client_id,
            max_age=service.REGISTRATION_COOKIE_SECONDS, httponly=True, secure=settings().cookie_secure,
            samesite="lax", path=service.REGISTRATION_COOKIE_PATH)
    except APIError as error:
        db.rollback()
        # Only our own controlled error codes may be used by the UI.
        result = error.code if error.code.startswith("chatgpt_") else "chatgpt_authorization_failed"
    except SQLAlchemyError:
        db.rollback()
        result = "chatgpt_authorization_failed"
    finally:
        for reference in db.info.pop("chatgpt_uncommitted_secrets", []):
            service.erase_secret(reference)
        response.delete_cookie(service.browser_cookie_name(), path=service.CALLBACK_PATH,
            httponly=True, secure=settings().cookie_secure, samesite="lax")
    response.headers["location"] = settings().public_origin + "/data?view=connections&chatgpt=" + result
    return response


@router.get("/api/v1/chatgpt/accounts", response_model=Envelope[s.ChatGPTAccountIndex], responses=READ_ERRORS, operation_id="chatgptAccounts")
def accounts(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    rows = db.scalars(select(ChatGPTAccount).where(ChatGPTAccount.owner_id == identity[0].id)
        .order_by(ChatGPTAccount.created_at, ChatGPTAccount.id)).all()
    return envelope(request, s.ChatGPTAccountIndex(items=[service.account_view(db, row) for row in rows]))


@router.delete("/api/v1/chatgpt/accounts/{account_id}", response_model=Envelope[s.ChatGPTDisconnectResult], responses=WRITE_ERRORS, operation_id="chatgptDisconnect")
def disconnect(account_id: UUID, request: Request, identity=Depends(csrf), db: Session = Depends(db_session)):
    account = service.get_owned_account(db, identity[0].id, account_id)
    unconfirmed = service.disconnect(db, account)
    return envelope(request, s.ChatGPTDisconnectResult(account=service.account_view(db, account), remote_revocation_unconfirmed=unconfirmed))


@router.post("/api/v1/chatgpt/accounts/{account_id}/welcome", response_model=Envelope[s.ChatGPTAccountView], responses=WRITE_ERRORS, operation_id="chatgptWelcome")
def welcome(account_id: UUID, request: Request, identity=Depends(csrf), db: Session = Depends(db_session)):
    from datetime import UTC, datetime
    account = service.get_owned_account(db, identity[0].id, account_id)
    if account.welcome_seen_at is None:
        account.welcome_seen_at = datetime.now(UTC)
        db.commit()
    return envelope(request, service.account_view(db, account))
