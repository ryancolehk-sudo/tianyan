"""Admission fence shared by HTTP writers, schedulers and the host updater."""
import hashlib
from functools import lru_cache

from fastapi import Depends, Request
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.auth import APIError
from app.config import settings

KEY = int.from_bytes(hashlib.sha256(b'tianyan:maintenance:admission:v1').digest()[:8], 'big', signed=True)


def available(db):
    return bool(db.scalar(text("SELECT to_regclass('system_settings_state')")))


def paused(db):
    return available(db) and bool(db.scalar(text('SELECT active FROM system_settings_state WHERE id=1')))


def admit(db):
    # Held until the entire request transaction closes, including write_once's
    # separate business transaction. The updater takes the exclusive counterpart.
    db.execute(text('SELECT pg_advisory_xact_lock_shared(:key)'), {'key': KEY})
    if paused(db):
        raise APIError(503, 'maintenance_active', '系统正在维护，请稍后重试。已保存的资料会保留。')


@lru_cache
def admission_engine():
    # Admission connections have their own pool. Holding the business pool while
    # another dependency waits for it could deadlock a burst of write requests.
    return create_engine(settings().database_url, pool_pre_ping=True, pool_size=2, max_overflow=8,
        connect_args={'connect_timeout': 5, 'options': '-c statement_timeout=10000'})


def request_admission(request: Request):
    if request.method not in {'GET', 'HEAD', 'OPTIONS'} or request.url.path == '/api/v1/chatgpt/callback':
        with Session(admission_engine()) as db:
            admit(db)
            yield
    else:
        yield


def begin_maintenance(db, job_id):
    from app.system_settings.service import get_system_state
    db.execute(text("SET LOCAL statement_timeout='600000'"))
    db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': KEY})
    state = get_system_state(db)
    state.active, state.upgrade_job_id, state.stage = True, job_id, 'draining'
    db.execute(text('UPDATE system_settings_state SET started_at=now() WHERE id=1'))
