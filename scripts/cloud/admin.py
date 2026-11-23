"""Host-authorized account management; JSON/password arrives only on stdin.

This file is mounted read-only into an isolated one-off API container by the
cloud operator. There is deliberately no public account-management endpoint.
"""
import json
import re
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import hasher, require_instance
from app.db import engine
from app.evidence.common import lock
from app.models import AuditEvent, User


def create_user(actor_name, username, password, role='researcher'):
    if not re.fullmatch(r'[a-z0-9_.-]{1,80}', username):
        raise ValueError('invalid_username')
    if role not in {'admin', 'researcher', 'viewer'}:
        raise ValueError('invalid_role')
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError('invalid_password')
    with Session(engine()) as db, db.begin():
        require_instance(db)
        actor = db.scalar(select(User).where(User.username == actor_name, User.enabled.is_(True)))
        if actor is None or actor.role != 'admin':
            raise ValueError('admin_required')
        lock(db, 'cloud:user:' + username)
        if db.scalar(select(User.id).where(User.username == username)):
            raise ValueError('user_exists')
        account = User(username=username, password_hash=hasher.hash(password), role=role)
        db.add(account)
        db.flush()
        db.add(AuditEvent(actor_id=actor.id, action='cloud.user.create', resource_type='user',
                          resource_id=account.id, changed_field_names=['username', 'role', 'enabled']))
        return {'id': str(account.id), 'username': account.username, 'role': account.role}


def source_scope_check():
    """Only counters leave the host; no configured source or credential data."""
    from app.models import Provider
    with Session(engine()) as db:
        blocked = list(db.scalars(select(Provider.id).where(
            Provider.enabled.is_(True), Provider.family.is_not(None),
            Provider.rights['redistribute'].astext.is_distinct_from('allowed'))))
        return {'shared_source_scope_ready': not blocked, 'blocked_sources': len(blocked)}


def main():
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ValueError('request_too_large')
        body = json.loads(raw)
        if body.get('action') == 'source-scope-check':
            result = source_scope_check()
        elif body.get('action') == 'create-user':
            result = create_user(body['actor'], body['username'], body['password'], body.get('role', 'researcher'))
        else:
            raise ValueError('unsupported_action')
        print(json.dumps(result, ensure_ascii=True))
    except Exception:
        # Do not return SQL errors or body values, which can contain credentials.
        print('{"error":"cloud_admin_operation_failed"}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
