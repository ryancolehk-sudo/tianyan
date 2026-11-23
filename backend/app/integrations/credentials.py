"""Bind existing source identities to private connection credentials.

A stored binding is authoritative even when disabled, archived, or unreadable.
Only sources that have never had a binding retain the legacy environment path.
"""
from dataclasses import dataclass
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select, text

from app.auth import APIError
from app.config import settings
from app.market.protocol import SourceFailure
from app.market.tushare_specs import allowed_endpoints
from app.market.tushare_specs import FAMILIES as TUSHARE_FAMILIES
from app.market.connected_specs import FUYAO_ENDPOINT, TDX_ENDPOINT, FUYAO_FAMILIES, TDX_FAMILIES


@dataclass(frozen=True)
class SourceCredentials:
    credential: SecretStr | None
    endpoint: str
    connection_id: UUID | None = None
    revision: int | None = None
    required: bool = False
    enabled: bool = True
    reason: str | None = None


def integration_table_present(db):
    """Older-schema upgrade fixtures intentionally do not have this table yet."""
    key = 'integrations.table_present'
    if key not in db.info:
        db.info[key] = db.scalar(text("SELECT to_regclass('service_connections')")) is not None
    return db.info[key]


def invalidate(db):
    for key in tuple(db.info):
        if isinstance(key,str) and key.startswith('integrations.') and key != 'integrations.table_present':
            db.info.pop(key, None)


def connections(db, *, refresh=False):
    if not integration_table_present(db):
        return []
    key = 'integrations.connections'
    if refresh or key not in db.info:
        from app.ai.models import Connection
        rows = list(db.scalars(select(Connection).where(Connection.kind.in_(['market', 'news']))
                               .order_by(Connection.archived, Connection.updated_at.desc(), Connection.id)))
        db.info[key] = rows
    return db.info[key]


def environment_reference(family):
    if family in TUSHARE_FAMILIES:
        return 'env:tushare'
    return {'mairui_funds': 'env:mairui', 'mairui_pools': 'env:mairui',
            'tickflow_live': 'env:tickflow'}.get(family)


def default_endpoint(family):
    if family in FUYAO_FAMILIES:
        return FUYAO_ENDPOINT
    if family in TDX_FAMILIES:
        return TDX_ENDPOINT
    if family in TUSHARE_FAMILIES:
        return settings().tushare_endpoint
    if family in {'mairui_funds', 'mairui_pools'}:
        return 'https://api.mairuiapi.com/'
    if family == 'tickflow_live':
        return 'https://api.tickflow.org/v1/quotes'
    return ''


def resolve_source_credentials(db, source_id, family, *, allow_disabled=False, refresh=False):
    identity = str(source_id)
    matches = [row for row in connections(db, refresh=refresh) if identity in row.source_ids]
    active = [row for row in matches if not row.archived]
    if len(active) > 1:
        return SourceCredentials(None, default_endpoint(family), required=bool(environment_reference(family)) or family in FUYAO_FAMILIES | TDX_FAMILIES,
                                 enabled=False, reason='integration_binding_conflict')
    row = active[0] if active else matches[0] if matches else None
    required = bool(environment_reference(family)) or family in FUYAO_FAMILIES | TDX_FAMILIES
    if row is None:
        ref = environment_reference(family)
        attr = {'env:tushare': 'tushare_token', 'env:mairui': 'mairui_licence',
                'env:tickflow': 'tickflow_api_key'}.get(ref)
        secret = getattr(settings(), attr, None) if attr else None
        return SourceCredentials(secret, default_endpoint(family), required=required,
                                 reason='credentials_missing' if required and not secret else None)
    enabled = bool(row.enabled and not row.archived)
    reason = 'source_disabled' if row.archived or (not row.enabled and not allow_disabled) else None
    endpoint = row.endpoint
    if family in TUSHARE_FAMILIES and endpoint not in allowed_endpoints():
        reason = 'endpoint_not_allowed'
    if family in {'mairui_funds', 'mairui_pools', 'tickflow_live'} | FUYAO_FAMILIES | TDX_FAMILIES and endpoint != default_endpoint(family):
        reason = 'endpoint_not_allowed'
    secret = None
    if row.secret_ref:
        try:
            from app.ai.vault import read_secret
            value = read_secret(row.secret_ref)
            secret = value if isinstance(value, SecretStr) else SecretStr(value) if value else None
        except APIError as error:
            reason = reason or error.code
    if required and not secret:
        reason = reason or 'credentials_missing'
    return SourceCredentials(secret, endpoint, row.id, row.revision, required, enabled, reason)


def job_context(credentials, source_id, *, probe=False):
    if credentials.connection_id is None:
        return None
    context = {'connection_id': str(credentials.connection_id), 'revision': credentials.revision,
               'source_id': str(source_id)}
    if probe:
        context['probe'] = True
    return context


def assert_source_binding(db, source_id, family, payload, *, frozen=None):
    context = payload.get('integration_context')
    probe = isinstance(context, dict) and context.get('probe') is True
    resolved = resolve_source_credentials(db, source_id, family, allow_disabled=probe, refresh=True)
    if resolved.reason:
        raise SourceFailure(resolved.reason)
    expected = job_context(resolved, source_id, probe=probe)
    if context != expected:
        raise SourceFailure('configuration_changed')
    if frozen is not None and (resolved.connection_id, resolved.revision, resolved.endpoint) != (
            frozen.connection_id, frozen.revision, frozen.endpoint):
        raise SourceFailure('configuration_changed')
    return resolved
