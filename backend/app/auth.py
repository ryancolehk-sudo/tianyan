import hashlib
import secrets
from urllib.parse import urlsplit
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Header, Request, Security
from fastapi.security import APIKeyCookie
from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db import db_session
from app.models import InstanceState, LoginSession, LoginThrottle, User

COOKIE = settings().session_cookie
cookie = APIKeyCookie(name=COOKIE, auto_error=False)
hasher = PasswordHasher()
_dummy_hash = hasher.hash(secrets.token_urlsafe(32))


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def desktop_origin():
    base = urlsplit(settings().public_origin)
    if base.scheme != "http" or not base.port or base.netloc != f"127.0.0.1:{base.port}" or base.path or base.query or base.fragment:
        raise APIError(403, "desktop_unavailable", "桌面入口仅适用于本机回环部署。")
    return base.netloc


def desktop_ticket_hash(token: str):
    return digest("desktop-ticket:" + token)


def require_instance(db: Session):
    mode = db.scalar(select(InstanceState.app_mode).where(InstanceState.id == 1))
    if mode != settings().app_mode:
        raise APIError(503, "instance_mode_mismatch", "实例模式与数据库不一致，请检查部署配置。")


def require_origin(request: Request):
    if request.headers.get("origin") != settings().public_origin:
        raise APIError(403, "origin_rejected", "请求来源不受信任，请从项目页面操作。")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise APIError(403, "origin_rejected", "不允许跨站操作。")


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def login_allowed(db: Session, username: str, address: str):
    now = datetime.now(UTC)
    db.execute(delete(LoginThrottle).where(LoginThrottle.resets_at < now - timedelta(days=1)))
    counts = []
    for key, limit in [(f"ip:{address}", 30), (f"user:{username}", 5)]:
        stmt = insert(LoginThrottle).values(bucket=digest(key), attempts=1, resets_at=now + timedelta(minutes=5))
        stmt = stmt.on_conflict_do_update(index_elements=[LoginThrottle.bucket], set_={
            "attempts": case((LoginThrottle.resets_at <= now, 1), else_=LoginThrottle.attempts + 1),
            "resets_at": case((LoginThrottle.resets_at <= now, now + timedelta(minutes=5)), else_=LoginThrottle.resets_at),
        }).returning(LoginThrottle.attempts)
        counts.append((db.execute(stmt).scalar_one(), limit))
    db.commit()
    if any(count > limit for count, limit in counts):
        raise APIError(429, "login_limited", "尝试次数过多，请五分钟后再试。")


def principal(request: Request, token: str | None = Security(cookie), db: Session = Depends(db_session)) -> tuple[User, LoginSession]:
    require_instance(db)
    row = db.execute(select(User, LoginSession).join(LoginSession, LoginSession.user_id == User.id).where(
        LoginSession.token_hash == digest(token), LoginSession.revoked_at.is_(None),
        LoginSession.expires_at > func.now(), User.enabled.is_(True),
    )).first() if token else None
    if not row:
        raise APIError(401, "unauthenticated", "请登录后继续。")
    return row[0], row[1]


def csrf(request: Request, x_csrf_token: str | None = Header(default=None), identity=Depends(principal)):
    require_origin(request)
    if not secrets.compare_digest(x_csrf_token or "", identity[1].csrf_token):
        raise APIError(403, "csrf_rejected", "页面凭证已失效，请刷新后再试。")
    return identity


def require_role(user: User, *roles: str):
    if user.role not in roles:
        raise APIError(403, "forbidden", "当前账号没有此操作权限。")
