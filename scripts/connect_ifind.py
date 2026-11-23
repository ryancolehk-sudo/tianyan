"""Run in the target backend environment. Never accepts a token on the command line."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))


def main():
    from datetime import UTC, datetime
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.ai import probes, service, vault
    from app.ai.models import Connection
    from app.ai.schemas import ConnectionInput
    from app.auth import APIError, require_instance
    from app.config import settings
    from app.db import engine
    from app.integrations.ifind_client import ENDPOINT
    from app.models import User

    parser = argparse.ArgumentParser(description='Import an already-authorized local iFinD skill key into Tianyan vault.')
    parser.add_argument('--skill-config', type=Path, required=True)
    parser.add_argument('--admin', required=True)
    parser.add_argument('--expected-mode', choices=['real', 'demo'], required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    if settings().app_mode != args.expected_mode or not vault.ready():
        raise APIError(409, 'target_not_ready', '目标环境或密钥存储不匹配。')
    # Local file only, no remote fetch, new credentials or printing.
    value = json.loads(args.skill_config.read_text(encoding='utf-8-sig')).get('auth_token')
    body = ConnectionInput(name='同花顺 iFinD 官方 MCP', kind='market', template='ifind_mcp', endpoint=ENDPOINT,
                           api_key=value, enabled=False)
    if not body.api_key or not body.api_key.get_secret_value():
        raise APIError(422, 'credentials_missing', '本机授权文件没有凭据。')
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        require_instance(db)
        user = db.scalar(select(User).where(User.username == args.admin, User.role == 'admin', User.enabled.is_(True)))
        if not user:
            raise APIError(403, 'admin_required', '需要目标环境已有管理员。')
        rows = list(db.scalars(select(Connection).where(Connection.template == 'ifind_mcp', Connection.archived.is_(False)).with_for_update()))
        if len(rows) > 1:
            raise APIError(409, 'ambiguous_connection', '请在页面选择已有接入，不能批量覆盖。')
        prior = rows[0] if rows else None
        if prior and prior.owner_id is not None:
            raise APIError(409, 'private_connection', '不覆盖已有个人接入。')
        if not args.apply:
            print(json.dumps({'ready': True, 'will_update_existing': bool(prior), 'will_enable_manual_queries': args.enable, 'collection_enabled': False}))
            return
        if prior:
            body = body.model_copy(update={'name': prior.name, 'expected_revision': prior.revision})
        view, _, _ = service.store_connection(db, user, body, identity=prior.id if prior else None)
        identity = view['id']
    with Session(engine()) as db, db.begin():
        row = db.get(Connection, identity)
        result, _, _ = probes.data_probe(db, user, probes.existing_input(row))
        if result['status'] != 'success':
            print(json.dumps({'connection_id': str(identity), 'enabled': False, 'probe': result['status'], 'error_code': result.get('error_code')}))
            return
        if args.enable:
            service.store_connection(db, user, probes.existing_input(row).model_copy(update={'enabled': True}), identity=row.id)
        print(json.dumps({'connection_id': str(identity), 'enabled': args.enable, 'probe': 'index_catalog_only', 'checked_at': datetime.now(UTC).isoformat(), 'automatic_collection': False}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not print arbitrary exception text: validation errors may contain input.
        print(json.dumps({'status': 'failed', 'code': getattr(error, 'code', 'local_setup_failed')}))
        sys.exit(1)
