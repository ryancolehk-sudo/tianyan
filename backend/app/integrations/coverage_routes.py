"""Apply a feature's routing repair only while its source evidence and user choices still match."""
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import APIError
from app.db import engine
from app.evidence.common import content_hash, lock, now
from app.integrations import routing
from app.integrations.inspection import WAIT_REASONS, guidance, source_hold
from app.integrations.inspection_repair import eligible, signature
from app.integrations.inspection_schemas import RepairReceipt
from app.integrations.routing_schemas import RouteChange
from app.maintenance_control import admit
from app.models import Job, User

FOLLOW = 'coverage.connect'


def permitted(option, supplier):
    return (option['implemented'] and option['billing'] in {'free', 'free_quota', 'subscription'}
            and supplier['enabled'] and (not supplier['credential_required'] or supplier['credential_configured'])
            and option['entitlement'] != 'locked' and option['state'] not in {'disabled', 'paused', 'permission_missing',
            'credentials_missing', 'points_not_enabled', 'not_configured'} and bool(option['source_ids']))


def ready(db, option):
    if option['entitlement'] != 'verified' or option['state'] != 'ready' or not option['last_success_at']:
        return False
    _, _, _, _, successes = routing._source_snapshot(db)
    for source in option['source_ids']:
        run = successes.get(UUID(source))
        if (run and run.received_at.isoformat() == option['last_success_at'] and run.status == 'complete'
                and run.request_count > 0 and not run.clock_regressed and not run.rejected
                and (run.accepted or 0) + (run.duplicates or 0) >= max(1, run.expected or 0)):
            return True
    return False


def route_choice(existing, port):
    return (existing.choices if existing else {}).get(port, {})


def probe_due(db, provider, config, last, clock):
    if last:
        return source_hold(db, provider, config, last, clock)
    from app.market.budget import family_cooldown_until
    from app.market.service import background_window, collection_window
    # The scheduler keeps advancing next_run_at while a route is unselected. Its first
    # explicit probe observes real market windows and quota, independently of that placeholder.
    deadlines = (family_cooldown_until(db, provider.family, clock), background_window(db, provider.family, clock),
                 collection_window(db, provider, config, clock))
    return max((due for due in deadlines if due and due > clock), default=None)


def select_verified(db, owner, cap, option, existing, snapshot, request_id=None):
    supplier = option['provider_id']
    if cap['effective_provider_id'] != supplier:
        choice = route_choice(existing, cap['id'])
        previous = [key for key in routing.selected_providers(choice) if key != supplier]
        change = RouteChange(capability_id=cap['id'], provider_id=supplier, fallback_provider_ids=previous,
                             allow_points=choice.get('allow_points', False))
        snapshot = routing._save_routes(db, owner, [change], existing, snapshot, request_id, activate_connections=False)
    return snapshot


def route_receipt(cap, provider, state, advice, **fields):
    return RepairReceipt(id='port:' + cap['id'], area='capability', title=cap['name'],
        capability=cap['id'], reason=provider, state=state, advice=advice, **fields)


def follow_route(db, owner, cap, option, provider, config, existing, clock, source_action, *, probe, append=False):
    intent = dict(port=cap['id'], provider=option['provider_id'], source=str(provider.id),
                  configuration=signature(db, provider, config), choice=content_hash(route_choice(existing, cap['id'])))
    job = db.scalar(select(Job).where(Job.kind == FOLLOW, Job.owner_id == owner.id,
        Job.state.in_(['queued', 'running']), Job.payload.contains(intent),
        Job.payload['append'].astext.is_not_distinct_from('true' if append else None)).order_by(Job.created_at).limit(1))
    if not job:
        due = clock if probe else max(clock, source_action.retry_at or clock)
        job = Job(owner_id=owner.id, kind=FOLLOW, state='queued', available_at=due,
                  payload={**intent, **({'append': True} if append else {}), 'probe': probe,
                           'collection': str(source_action.job_id) if source_action.job_id else None})
        db.add(job)
        db.flush()
    return route_receipt(cap, option['provider_id'], 'waiting', '后台将核验该来源并自动选用。', job_id=job.id,
                         source_id=provider.id, retry_at=job.available_at)


def reassess(db, job, clock):
    from app.ai.models import Connection
    from app.integrations.data import probe_data
    from app.market.service import source_states
    payload = job.payload
    owner = db.get(User, job.owner_id)
    if not owner or not owner.enabled or owner.role != 'admin':
        raise APIError(403, 'permission_missing', '修复账号权限已变化。')
    provider, config = eligible(db, UUID(payload['source']))
    if config.paused_reason or signature(db, provider, config) != payload['configuration']:
        raise APIError(409, 'configuration_changed', '来源设置已变化，请按当前设置重新修复。')
    lock(db, 'data-center:routes')
    existing = routing.route_settings(db)
    if content_hash(route_choice(existing, payload['port'])) != payload['choice']:
        raise APIError(409, 'routing_changed', '此数据项的来源顺序已变更，请核对当前选择。')
    snapshot = routing.matrix(db)
    cap = next(c for c in snapshot['capabilities'] if c['id'] == payload['port'])
    key = payload['provider']
    option = next(o for o in cap['options'] if o['provider_id'] == key)
    supplier = next(p for p in snapshot['providers'] if p['id'] == key)
    if not permitted(option, supplier) or str(provider.id) not in option['source_ids']:
        raise APIError(409, 'source_unavailable', '该来源当前无法供数，请检查启用状态、权限和采集范围。')
    effective = next((o for o in cap['options'] if o['provider_id'] == cap['effective_provider_id']), None)
    if not payload.get('append') and effective and ready(db, effective):
        return '已按当前有效回执核验供数来源。', None
    collection = db.get(Job, UUID(payload['collection'])) if payload.get('collection') else None
    if collection and collection.state in {'queued', 'running'}:
        return None, max(clock + timedelta(minutes=1), collection.available_at)
    if ready(db, option):
        if payload.get('append'):
            from app.integrations.routing_schemas import ConfigurePorts
            routing.configure_ports(db, owner, ConfigurePorts(expected_revision=snapshot['revision'],
                provider_id=key, capability_id=cap['id']))
        else:
            select_verified(db, owner, cap, option, existing, snapshot)
        return '已核验有效回执并选用' + supplier['name'] + '。', None
    if collection and collection.state in {'failed', 'cancelled', 'succeeded'}:
        if collection.error_code in WAIT_REASONS:
            job.payload = {**payload, 'collection': None}
            return None, max(clock + timedelta(minutes=1), config.next_run_at)
        raise APIError(409, collection.error_code or 'incomplete_receipt', '本次取数未能证明此端口完整可用。请检查该来源最近回执。')
    if payload['probe']:
        source = next(s for s in source_states(db) if s.provider.id == provider.id)
        due = probe_due(db, provider, config, source.last_attempt, clock)
        if due:
            return None, due
        conn = db.get(Connection, UUID(supplier['connection_id'])) if supplier['connection_id'] else None
        if not conn or not conn.enabled:
            raise APIError(409, 'connection_required', '请先保存并启用该数据来源的连接。')
        result = probe_data(db, dict(id=conn.id, template=conn.template, endpoint=conn.endpoint,
            source_ids=conn.source_ids, expected_revision=conn.revision, source_id=str(provider.id)))
        job.payload = {**payload, 'collection': result['job_id']}
    return None, max(clock + timedelta(minutes=1), config.next_run_at if not payload['probe'] else clock)


def process_follow(claimed):
    with Session(engine()) as db, db.begin():
        admit(db)
        clock = now(db)
        job = db.scalar(select(Job).where(Job.id == claimed.id, Job.state == 'running',
            Job.lease_token == claimed.lease_token, Job.lease_until > clock).with_for_update())
        if not job:
            return
        db.execute(text("SET LOCAL lock_timeout = '2s'"))
        try:
            with db.begin_nested():
                if clock > job.created_at + timedelta(days=7):
                    raise APIError(409, 'verification_timeout', '七天内未取得有效回执，请检查来源后重新修复。')
                message, due = reassess(db, job, clock)
        except APIError as error:
            due = clock + timedelta(minutes=2) if error.code in WAIT_REASONS else None
            message = error.message or guidance(error.code)
            job.error_code = error.code
        if due:
            # A scheduled wake is not a failed lease attempt.
            job.state, job.attempts, job.available_at = 'queued', 0, min(due, job.created_at + timedelta(days=7))
        else:
            job.state = 'failed' if job.error_code else 'succeeded'
            job.completed_at, job.result_ref = clock, {'advice': message}
        job.lease_token = job.lease_until = None
