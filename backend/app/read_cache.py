"""Durable, authenticated read projections with cheap conditional validation.

Only registered pure reads may use this cache. Ingestion never runs here. A
database lease coordinates API cold builds and the separate projection worker.
"""
import copy
import hashlib
import json
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import and_, case, delete, func, not_, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, defer

from app.auth import APIError
from app.config import settings
from app.models import InstanceState, Provider, User
from app.read_cache_models import ReadCache, ReadCacheDispatch, ReadDependency

FORMAT = 'read-v19-independent-market-series'
NEWS_SECURITIES_FORMAT = 'news-securities-1'
SHANGHAI = ZoneInfo('Asia/Shanghai')
RETENTION = timedelta(days=7)
LEASE = timedelta(seconds=120)
INTERACTIVE_RECENCY = timedelta(minutes=5)
FOCUS_RECENCY = timedelta(seconds=15)
OPERATIONS = {'workbench', 'details', 'review-day', 'review-series', 'review-lhb',
              'review-stocks', 'review-theme-history', 'review-analysis', 'review-rotation', 'review-sector-atlas', 'review-overview', 'review-heights', 'review-new-highs', 'news-items', 'news-editions', 'news-edition'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(jsonable_encoder(value), sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def cache_scope(db, user):
    if not settings().read_cache_enabled:
        return None
    instance = db.execute(select(InstanceState.cache_id, InstanceState.app_mode)
                          .where(InstanceState.id == 1)).one()
    rights = db.execute(select(Provider.id, Provider.family, Provider.kind, Provider.enabled,
        Provider.configured, Provider.entitlement_status, Provider.rights, Provider.capabilities,
        Provider.adapter_version).order_by(Provider.id)).all()
    authorization = db.scalar(select(ReadDependency.revision).where(ReadDependency.key == 'authorization')) or 0
    return fingerprint([FORMAT, settings().deployment_mode, str(instance[0]), instance[1], str(user.id), user.role,
                        user.enabled, authorization, [list(row) for row in rights]])


def dependency_keys(operation, owner_id, params=None):
    if operation.startswith('review-'):
        from app.market.review_protocol import DATASET_FAMILIES
        families = {family for key, family in DATASET_FAMILIES.items()
                    if key not in {'emotion_history', 'stock_funds', 'market_funds'}} | {'tushare_market_rt', 'tushare_rt_indices', 'mairui_pools'}
        if operation == 'review-sector-atlas':
            families.update({'direction_kpl_strength','tushare_ths_index','tushare_ths_daily'})
        if operation == 'review-overview':
            families.update({'tushare_moneyflow_dc', 'tushare_moneyflow_mkt_dc', 'direction_kpl_emotion', 'duanxianxia_auction', 'duanxianxia_auction_history',
                             'tencent_auction', 'tushare_ths_member', 'tushare_ths_index'})
        if operation == 'review-new-highs':
            families = {'review_kpl_new_highs','tushare_calendar_sse','tushare_calendar_szse',
                        'tushare_daily','tushare_daily_basic','tushare_moneyflow_dc'}
        if operation == 'review-analysis' and (params or {}).get('kind') == 'ladder':
            families.add('tushare_limit_step')
        from app.market.calendar_selection import FAMILIES as CALENDAR_FAMILIES
        families.update(CALENDAR_FAMILIES)
        return ['authorization', f'user:{owner_id}'] + [f'family:{family}' for family in sorted(families)]
    if operation.startswith('news-'):
        keys = ['authorization', 'news', f'news-user:{owner_id}', f'user:{owner_id}']
        if operation in {'news-items', 'news-edition'}:
            # Issuer metadata may also arrive in an exact-ID announcement
            # reference, rather than as a new news evidence version.
            keys.append('family:cninfo_announcements')
        if operation == 'news-items':
            from app.market.news_feeds import NEWS_FAMILIES
            keys += [f'run:{family}' for family in sorted(NEWS_FAMILIES)]
        return keys
    from app.market.terminal_read import ALL_FAMILIES
    # Feed health is read separately from the recovery endpoint. Failed polls
    # do not change market values and must not rebuild every large page.
    focused_funds = bool(operation == 'workbench' and params and params.get('view') in {'overview','funds'}
        and not params.get('as_of_time') and params.get('day') in (None,datetime.now(UTC).astimezone(SHANGHAI).date().isoformat()))
    # A pinned detail is a completed parent's evidence snapshot. Later market
    # receipts cannot enter it. Current provider/source-version authorization,
    # routing, catalog and owner changes still invalidate it immediately.
    families=[] if operation=='details' and params and params.get('observation_cutoff') else sorted(ALL_FAMILIES)
    return (['funds-selection:'+str(owner_id)] if focused_funds else []) + ['authorization', 'catalog', 'routing-health', f'user:{owner_id}'] + [f'family:{family}' for family in families] + (
        [f'watch:{owner_id}'] if operation == 'workbench' else [])


def ttl(operation, params=None):
    params=params or {}
    if operation=='details' and params.get('observation_cutoff'):
        # Retired contexts must not keep rebuilding in the current-data queue.
        # Rights/scope are checked on every GET; dependency changes still wake it.
        return int(RETENTION.total_seconds())
    if operation=='workbench' and params.get('view')=='auction' and not params.get('as_of_time'):
        local=datetime.now(UTC).astimezone(SHANGHAI)
        if (not params.get('day') or str(params['day'])==local.date().isoformat()) and clock_time(9,14)<=local.time()<clock_time(9,36):
            return 5
    if operation in {'workbench','details'} and not params.get('as_of_time'):
        local=datetime.now(UTC).astimezone(SHANGHAI)
        if (not params.get('day') or str(params['day'])==local.date().isoformat()) and clock_time(9,15)<=local.time()<clock_time(15,1):
            return 15
    return 300 if operation.startswith('review-') else 60


def normalized_params(params):
    return jsonable_encoder({key: value for key, value in params.items() if value is not None})


def visibility_boundary(payload):
    value=payload.get('_next_visibility_at') if isinstance(payload,dict) else None
    if not isinstance(value,str):return None
    try:stamp=datetime.fromisoformat(value)
    except ValueError:return None
    return stamp if stamp.utcoffset() is not None else None


def visibility_epoch(payload, moment):
    epoch=payload.get('_visibility_epoch',0) if isinstance(payload,dict) else 0
    boundary=visibility_boundary(payload)
    return epoch+int(boundary is not None and moment>=boundary)


def visibility_now(db):
    # The projection's receipt cutoff is produced by this database clock.
    # Process-clock skew must neither unlock a future row nor postpone it.
    return db.scalar(select(func.clock_timestamp()))


def refresh_deadline(payload, operation, params, moment):
    deadline=moment+timedelta(seconds=ttl(operation,params))
    boundary=visibility_boundary(payload)
    return min(deadline,boundary) if boundary is not None else deadline


def revision_manifest(db, entry, moment=None):
    rows = dict(db.execute(select(ReadDependency.key, ReadDependency.revision).where(
        ReadDependency.key.in_(entry.dependency_keys))).all())
    # Date and market phase affect visibility even when no data arrives. Explicit
    # historical cutoffs remain fixed; a new receipt can never enter an old view.
    moment=moment or datetime.now(UTC)
    local = moment.astimezone(SHANGHAI)
    as_of = entry.params.get('as_of_time')
    observation = entry.params.get('observation_cutoff') if entry.operation=='details' else None
    selected_day = entry.params.get('day') or entry.params.get('trade_day')
    fixed_day = selected_day and selected_day < local.date().isoformat()
    # Completed historical queries and immutable edition details do not change
    # when today's clock crosses a market boundary.
    timeless = fixed_day or entry.operation == 'news-edition'
    if as_of or observation or timeless:
        temporal = as_of or observation or 'fixed'
    else:
        temporal = [local.date().isoformat(), sum(local.time() >= boundary for boundary in (
            clock_time(9),clock_time(9, 15), clock_time(9, 25), clock_time(9, 30), clock_time(11, 30),
            clock_time(13), clock_time(15), clock_time(15, 5)))]
    format_version = FORMAT
    if entry.operation.startswith('review-') or entry.operation in {'workbench','details'}:
        from app.market.calendar_selection import READ_VERSION
        format_version += '-' + READ_VERSION
    if entry.operation in {'news-items', 'news-edition'}:
        format_version += '-' + NEWS_SECURITIES_FORMAT
    if entry.operation == 'review-overview':
        format_version += '-postclose-2'
    if entry.operation in {'review-day', 'review-series'}:
        format_version += '-review-expansion-1'
    elif entry.operation in {'workbench', 'details'}:
        format_version += '-quote-speed-1'
    if entry.operation == 'review-analysis' and entry.params.get('kind') == 'ladder':
        format_version += '-saved-ladder-1'
    if entry.operation == 'workbench' and entry.params.get('view') == 'ladder':
        format_version += '-intraday-ladder-2-first-board'
    if entry.operation == 'workbench' and entry.params.get('view') == 'overview':
        format_version += '-overview-current-ladder-2-first-board-breadth-minute-1'
    if entry.operation == 'workbench' and entry.params.get('view') == 'funds':
        format_version += '-sector-strength-1'
    if entry.operation == 'workbench':
        format_version += '-funds-universe-2'
    return dict(format=format_version, operation=entry.operation, scope=entry.scope, params=entry.params,
                dependencies={key: rows.get(key, 0) for key in sorted(entry.dependency_keys)}, temporal=temporal,
                visibility=visibility_epoch(entry.payload,
                    visibility_now(db) if visibility_boundary(entry.payload) is not None else moment))


def desired_revision(db, entry, moment=None):
    return fingerprint(revision_manifest(db, entry, moment))


def data_advance_only(before, after):
    """A complete older data snapshot may progress, never cross an access/time boundary."""
    if any(before[key] != after[key] for key in before if key != 'dependencies'):
        return False
    prior, current = before['dependencies'], after['dependencies']
    if prior.keys() != current.keys():
        return False
    return all(current[key] >= revision and
               (current[key] == revision or key != 'authorization' and not key.startswith('user:'))
               for key, revision in prior.items())


def get_entry(db, key, *, metadata_only=False):
    query = select(ReadCache).where(ReadCache.key == key).execution_options(populate_existing=True)
    if metadata_only:
        query = query.options(defer(ReadCache.payload))
    return db.scalar(query)


def cache_key(scope, operation, params):
    """Use one read identity for stored entries and publication selection."""
    params = normalized_params(params)
    identity = [scope, operation, params]
    if operation.startswith('review-') or operation in {'workbench','details'}:
        from app.market.calendar_selection import READ_VERSION
        # Retained entries never acquire new dependency keys implicitly.
        # Give these reads a new identity without changing session rights.
        identity.append(READ_VERSION)
    if operation in {'news-items', 'news-edition'}:
        # As with upgraded ladder reads below, a fresh entry also acquires its
        # new family dependency without changing anyone's authorization scope.
        identity.append(NEWS_SECURITIES_FORMAT)
    if operation == 'review-analysis' and params.get('kind') == 'ladder':
        # Give upgraded ladder reads their new source dependency without
        # invalidating other pages or reusing old cached empty columns.
        identity.append('saved-ladder-1')
    return fingerprint(identity)


def ensure_entry(db, user, scope, operation, params, *, metadata_only=False):
    params = normalized_params(params)
    key = cache_key(scope, operation, params)
    moment = datetime.now(UTC)
    existing = get_entry(db, key, metadata_only=metadata_only)
    if existing is not None:
        # A collector marks entries dirty in its ingestion transaction. Hot
        # reads must not wait behind that write lock just to touch retention.
        if existing.accessed_at < moment - timedelta(seconds=60):
            mutate_if_free(db, key, dict(accessed_at=moment))
        return existing
    dependencies = dependency_keys(operation, user.id, params)
    db.execute(insert(ReadDependency).values([dict(key=key, revision=0, last_txid=0) for key in dependencies])
               .on_conflict_do_nothing(index_elements=['key']))
    db.execute(insert(ReadCache).values(key=key, owner_id=user.id, scope=scope, operation=operation,
        params=params, dependency_keys=dependencies, dirty=True,
        refresh_after=moment, accessed_at=moment).on_conflict_do_nothing(index_elements=['key']))
    db.execute(update(ReadCache).where(ReadCache.key == key).values(accessed_at=moment))
    db.commit()
    return get_entry(db, key, metadata_only=metadata_only)


def mutate_if_free(db, key, values):
    locked = db.scalar(select(ReadCache.key).where(ReadCache.key == key)
                       .with_for_update(skip_locked=True))
    if locked:
        db.execute(update(ReadCache).where(ReadCache.key == key).values(**values))
    db.commit()
    return bool(locked)


def mark_visible(db, entry, request):
    """An authorized live GET may hint scheduling, never trigger collection.

    Speculative preloads omit the header. Coalesce hints and skip a locked row
    so painting an existing result cannot wait for another transaction.
    """
    moment = datetime.now(UTC)
    params = entry.params
    if (request.headers.get('x-workbench-active') != '1'
        or entry.operation not in {'workbench', 'details'}
        or params.get('as_of_time')
        or params.get('day') not in (None, moment.astimezone(SHANGHAI).date().isoformat())
        or entry.operation == 'details' and len(params.get('detail_codes', [])) > 1
        or entry.focused_at is not None and entry.focused_at >= moment - timedelta(seconds=5)):
        return
    mutate_if_free(db, entry.key, dict(focused_at=moment))


def claim(db, key, *, moment=None):
    moment = moment or datetime.now(UTC)
    token = uuid.uuid4()
    claimed = db.execute(update(ReadCache).where(ReadCache.key == key,
        or_(ReadCache.lease_until.is_(None), ReadCache.lease_until <= moment))
        .values(lease_token=token, lease_until=moment + LEASE,
                last_attempt_at=func.clock_timestamp()).returning(ReadCache.key)).scalar_one_or_none()
    db.commit()
    return token if claimed else None


def build(db, key, token):
    # Never let a caller's previous READ COMMITTED transaction choose the build
    # snapshot. Projections read many tables and must share one MVCC snapshot.
    db.rollback()
    try:
        with Session(db.get_bind(), expire_on_commit=False) as snapshot:
            snapshot.connection(execution_options={
                'isolation_level': 'REPEATABLE READ', 'postgresql_readonly': True})
            entry = get_entry(snapshot, key)
            if not entry or entry.lease_token != token:
                return False
            moment = datetime.now(UTC)
            if entry.lease_until is None or entry.lease_until <= moment:
                return release_obsolete(db, key, token)
            user = snapshot.scalar(select(User).where(User.id == entry.owner_id))
            authorized = bool(user and user.enabled and cache_scope(snapshot, user) == entry.scope)
            if authorized:
                manifest = revision_manifest(snapshot, entry, moment)
                revision = fingerprint(manifest)
                unchanged = entry.payload is not None and entry.revision == revision
                refreshed = None
                if not unchanged:
                    from app.market.fund_projection import refresh_selected_funds
                    refreshed = refresh_selected_funds(snapshot, entry, manifest)
                payload = (json.loads(json.dumps(entry.payload, ensure_ascii=False)) if unchanged else refreshed if refreshed is not None else
                    compute(snapshot, entry.owner_id, entry.operation, entry.params))
                payload['_visibility_epoch']=manifest['visibility']
                # Private derived metadata, omitted by render(). It allows a
                # later worker/process to identify the one changed dependency.
                payload['_dependency_manifest'] = copy.deepcopy(manifest)
                # Keep plain values; no ORM identity or transaction crosses
                # into the independent current authorization/lease check.
                candidate = dict(owner_id=entry.owner_id, scope=entry.scope, operation=entry.operation,
                    params=copy.deepcopy(entry.params), dependencies=list(entry.dependency_keys),
                    payload=payload, revision=revision, manifest=manifest,
                    built_at=entry.built_at if unchanged else moment, observed_at=moment)
        if not authorized:
            db.execute(delete(ReadCache).where(ReadCache.key == key, ReadCache.lease_token == token))
            db.commit()
            return False
        # Lock the dependency version rows before publishing. Ingestion either
        # precedes this check or queues this result after publication atomically.
        # Preserve the trigger's dependency-before-cache lock order.
        db.execute(select(ReadDependency).where(ReadDependency.key.in_(candidate['dependencies']))
                   .order_by(ReadDependency.key).with_for_update()).all()
        current = db.scalar(select(ReadCache).options(defer(ReadCache.payload)).where(ReadCache.key == key)
            .with_for_update().execution_options(populate_existing=True))
        checked_at = datetime.now(UTC)
        if not current or current.lease_token != token:
            db.rollback()
            return False
        if current.lease_until is None or current.lease_until <= checked_at:
            return release_obsolete(db, key, token)
        user = db.scalar(select(User).where(User.id == candidate['owner_id'])
                         .execution_options(populate_existing=True))
        if not user or not user.enabled or cache_scope(db, user) != candidate['scope']:
            db.execute(delete(ReadCache).where(ReadCache.key == key, ReadCache.lease_token == token))
            db.commit()
            return False
        current_manifest = revision_manifest(db, current, checked_at)
        visibility_checked_at=visibility_now(db) if (visibility_boundary(candidate['payload']) is not None
            or visibility_boundary(current.payload) is not None) else checked_at
        if visibility_epoch(candidate['payload'],visibility_checked_at)!=candidate['manifest']['visibility']:
            return release_obsolete(db,key,token)
        if not data_advance_only(candidate['manifest'], current_manifest):
            return release_obsolete(db, key, token)
        advancing = current_manifest != candidate['manifest']
        values = dict(payload=candidate['payload'], revision=candidate['revision'], built_at=candidate['built_at'],
            validated_at=candidate['observed_at'] if advancing else checked_at,
            refresh_after=checked_at if advancing else refresh_deadline(
                candidate['payload'],candidate['operation'],candidate['params'],visibility_checked_at),
            dirty=advancing, last_error=None, lease_token=None, lease_until=None)
        # Recheck effective lease at the write, including time spent taking
        # locks and checking current rights. A replaced/expired holder cannot
        # overwrite even a perfectly complete candidate.
        result = db.execute(update(ReadCache).where(ReadCache.key == key, ReadCache.lease_token == token,
            ReadCache.lease_until > func.clock_timestamp()).values(**values))
        if result.rowcount and not unchanged and candidate['operation'] in {'workbench', 'details'}:
            # PostgreSQL delivers only after this publication commits. No market
            # values leave the database through the notification channel.
            db.execute(text("SELECT pg_notify('tianyan_workbench', :payload)"),
                {'payload': json.dumps({'owner': str(candidate['owner_id']), 'scope': candidate['scope']})})
        db.commit()
        return bool(result.rowcount)
    except Exception as exc:
        db.rollback()
        # Error text can contain a source payload or query parameter. Save a
        # bounded class/code only; retained results keep their original clocks.
        code = exc.code if isinstance(exc, APIError) else type(exc).__name__
        if isinstance(exc, APIError) and exc.status in (401, 403, 404, 422):
            db.execute(delete(ReadCache).where(ReadCache.key == key, ReadCache.lease_token == token))
            db.commit()
            raise
        db.execute(update(ReadCache).where(ReadCache.key == key, ReadCache.lease_token == token).values(
            dirty=True, lease_token=None, lease_until=None, last_error=code[:50],
            refresh_after=datetime.now(UTC) + timedelta(seconds=30)))
        db.commit()
        raise


def release_obsolete(db, key, token):
    db.execute(update(ReadCache).where(ReadCache.key == key, ReadCache.lease_token == token).values(
        dirty=True, lease_token=None, lease_until=None, refresh_after=datetime.now(UTC)))
    db.commit()
    return False


def cached_endpoint(request, db, user, operation, params):
    if operation not in OPERATIONS:
        raise ValueError('unregistered read operation')
    if operation in {'workbench', 'details'}:
        # Reject impossible contexts before creating a durable entry. Otherwise
        # a background 422 would delete it and the browser would queue forever.
        observation = params.get('observation_cutoff')
        if observation is not None and (operation!='details' or params.get('as_of_time') is not None):
            raise APIError(422,'conflicting_cutoffs','详情资料截止与历史回看不能同时指定。')
        # The parent boundary is produced by the database clock. Validate it
        # against that same clock before creating a durable background entry.
        moment = db.scalar(select(func.clock_timestamp())) if observation is not None else datetime.now(UTC)
        cutoff = params.get('as_of_time') or observation
        if cutoff is not None and cutoff > moment:
            raise APIError(422, 'future_cutoff', '观察时点不能晚于服务器当前时间。')
        day = params.get('day')
        if day is not None and day > (cutoff or moment).astimezone(SHANGHAI).date():
            raise APIError(422, 'future_trade_day', '观察日期不能晚于当前日期或所选观察时点。')
    scope = cache_scope(db, user)
    if scope is None:
        payload = compute(db, user.id, operation, normalized_params(params))
        return render(request, payload)
    entry = ensure_entry(db, user, scope, operation, params)
    mark_visible(db, entry, request)
    # build() deliberately ends the caller's old transaction. Keep the key
    # independent of an expired ORM row, including a scope-revoked cold build.
    entry_key = entry.key
    if entry.payload is not None and not valid_payload(entry.payload, entry):
        db.execute(update(ReadCache).where(ReadCache.key == entry.key).values(payload=None,
            revision=None, dirty=True, lease_token=None, lease_until=None, refresh_after=datetime.now(UTC)))
        db.commit()
        entry = get_entry(db, entry_key)
    expected = desired_revision(db, entry)
    moment = datetime.now(UTC)
    if entry.payload is None:
        historical = params.get('as_of_time') is not None or (
            params.get('day') is not None and params['day'] < moment.astimezone(SHANGHAI).date())
        if operation in {'workbench', 'review-overview'} or operation == 'details' and not historical:
            # API threads never compute a cold page or wait behind its builder.
            # The durable entry is already queued for the independent worker.
            raise APIError(503, 'read_cache_warming', '资料正在后台准备，页面将自动更新。')
        token = claim(db, entry_key)
        if token:
            build(db, entry_key, token)
        else:
            # Another API process already builds this exact cold result. No
            # duplicate compute, and no long global preparation is introduced.
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                db.rollback()
                entry = get_entry(db, entry_key)
                if not entry or entry.payload is not None or entry.lease_token is None:
                    break
                time.sleep(0.05)
        entry = get_entry(db, entry_key)
        if not entry or entry.payload is None:
            raise APIError(503, 'read_cache_warming', '资料正在后台准备，请稍后重试。')
        expected = desired_revision(db, entry)
    matching = entry.revision == expected
    if matching and (entry.dirty or entry.refresh_after <= moment):
        # A metadata-only validation of unchanged dependencies is immediate.
        mutate_if_free(db, entry.key, dict(dirty=False, validated_at=moment,
            refresh_after=refresh_deadline(entry.payload,operation,params,
                visibility_now(db) if visibility_boundary(entry.payload) is not None else moment), last_error=None))
        entry = get_entry(db, entry.key)
    # Re-evaluate after the optional metadata transaction: an ingestion can
    # commit between the first version read and a nonblocking retention touch.
    matching = entry.revision == desired_revision(db, entry)
    if not matching and not entry.dirty:
        mutate_if_free(db, entry.key, dict(dirty=True, refresh_after=moment))
        entry = get_entry(db, entry.key)
    # Recheck current permissions after a potentially slow cold build. Never
    # leak an old permission scope while a revocation commits in parallel.
    db.refresh(user)
    current_scope = cache_scope(db, user)
    if not user.enabled or current_scope != scope:
        raise APIError(403, 'read_scope_changed', '资料读取权限已更新，请重新进入页面。')
    state = 'fresh' if matching else 'stale' if entry.last_error else 'refreshing'
    representation_revision = entry.revision
    if quote_aged(entry.payload):
        representation_revision = fingerprint([entry.revision, 'quote-aged'])
    meta = dict(revision=representation_revision, built_at=entry.built_at,
                validated_at=entry.validated_at, state=state)
    etag = '"' + representation_revision + '"'
    headers = {'ETag': etag, 'Cache-Control': 'private, no-cache', 'X-Cache-Scope': scope,
        'X-Read-Cache-State': state, 'X-Read-Cache-Built-At': entry.built_at.isoformat(),
        'X-Read-Cache-Validated-At': entry.validated_at.isoformat(), 'Vary': 'Cookie'}
    tags = [tag.strip().removeprefix('W/') for tag in request.headers.get('if-none-match', '').split(',')]
    if matching and (etag in tags or '*' in tags):
        return Response(status_code=304, headers=headers)
    return render(request, entry.payload, meta, headers)


def render(request, payload, read_meta=None, headers=None):
    # Stored JSONB and selected overlays are already JSON values. Only the
    # response metadata needs datetime/UUID encoding; traversing every chart
    # point again delays otherwise small updates.
    body = {'data': payload['data'], 'meta': {
        'request_id': str(request.state.request_id), 'app_mode': settings().app_mode,
        'generated_at': payload.get('built_at') or datetime.now(UTC),
        'as_of_time': payload.get('as_of_time'), 'quality': payload['quality']}}
    if read_meta:
        body['meta']['read_cache'] = read_meta
        body['meta']['generated_at'] = read_meta['built_at']
        # A cached live quote cannot remain labelled live after its freshness
        # window. This is only presentation; original data clocks are retained.
        if quote_aged(payload):
            body['data'] = {**body['data'], 'mode': 'review'}
    body['meta'] = jsonable_encoder(body['meta'])
    return JSONResponse(content=body, headers=headers)


def quote_aged(payload):
    data = payload.get('data')
    if not isinstance(data, dict) or data.get('mode') != 'live':
        return False
    latest = data.get('latest_at')
    return bool(latest and datetime.now(UTC) - datetime.fromisoformat(latest) > timedelta(seconds=180))


def valid_payload(payload, entry):
    return (isinstance(payload, dict) and 'data' in payload and isinstance(payload.get('quality'), dict)
            and payload['quality'].get('status') in {'complete', 'partial', 'empty', 'unavailable', 'stale', 'error'}
            and isinstance(payload['quality'].get('reasons'), list) and bool(entry.revision)
            and entry.built_at is not None and entry.validated_at is not None)


def compute(db, owner_id, operation, params):
    """Registered pure reads return JSON values, ready for persistence."""
    p = dict(params)
    for name in ('day', 'trade_day', 'start_day'):
        if p.get(name):
            p[name] = date.fromisoformat(p[name])
    for name in ('as_of_time','observation_cutoff'):
        if p.get(name):
            p[name] = datetime.fromisoformat(p[name])
    if p.get('instrument_id'):
        p['instrument_id'] = uuid.UUID(p['instrument_id'])
    cutoff = p.get('as_of_time')
    reasons = []
    recheck_at=None
    if operation in {'workbench', 'details'}:
        from app.market.terminal_read import workbench_view
        live_day=p.get('day') in (None,datetime.now(UTC).astimezone(SHANGHAI).date())
        recheck_at=[] if live_day and not p.get('as_of_time') and not p.get('observation_cutoff') else None
        result = workbench_view(db, owner_id, **p, recheck_at=recheck_at)
        status = 'partial' if result.stocks or (result.members if operation == 'details' else result.sectors or result.indices) else 'empty'
        reasons = ['只读取本机已保存且有权展示的资料；来源时间、接收时间和历史观察截止分别保留，不触发采集或模型。']
    elif operation.startswith('review-'):
        from app.market.review_details import stocks_view, theme_history_view
        from app.market.review_read import daily_view, lhb_view
        from app.market.review_series import series_view
        from app.market.review_analysis import analysis_view
        from app.market.direction_read import rotation_view
        from app.market.sector_atlas import sector_atlas
        from app.market.postclose import overview_view
        from app.market.height_history import height_history_view
        from app.market.new_highs import new_highs_view
        functions = {'review-day': daily_view, 'review-series': series_view, 'review-lhb': lhb_view,
                     'review-stocks': stocks_view, 'review-theme-history': theme_history_view,
                     'review-analysis': analysis_view, 'review-rotation': rotation_view, 'review-sector-atlas': sector_atlas,
                     'review-overview': overview_view, 'review-heights': height_history_view, 'review-new-highs':new_highs_view}
        result = functions[operation](db, **p)
        state = result.get('state')
        status = 'complete' if state == 'complete' else 'empty' if state in {'empty', 'missing'} else 'unavailable' if state == 'unavailable' else 'partial'
        reasons = ['只读本机已核实来源缓存；源数据日、接收时间与研究判断分别保存。历史补采不代表当时已经收到。']
    else:
        from app.news_center import read
        if operation == 'news-items':
            result = read.feed(db, owner_id, **p)
            status = 'partial' if result.items else 'empty'
            reasons = ['仅读取已保存资讯；自动汇编不等于投资判断']
        elif operation == 'news-editions':
            result = read.editions(db, owner_id, **p)
            status = 'complete' if result.items else 'empty'
        elif operation == 'news-edition':
            result = read.edition_detail(db, owner_id, uuid.UUID(p['edition_id']))
            status, reasons, cutoff = result.edition.coverage_status, [result.edition.coverage_note], result.edition.window_end
        else:
            raise ValueError('unregistered read operation')
    # Pydantic's JSON serializer has already converted every nested field.
    # Walking its output through the generic encoder again copies thousands of
    # chart points without changing them. Dictionary-based reads still need
    # the generic conversion for their native dates/UUIDs/decimals.
    data = result.model_dump(mode='json', by_alias=True) if isinstance(result, BaseModel) else jsonable_encoder(result)
    payload={'data': data, 'quality': {'status': status, 'reasons': reasons},
             'as_of_time': jsonable_encoder(cutoff)}
    if recheck_at:payload['_next_visibility_at']=min(recheck_at).isoformat()
    return payload


def interactive_condition(moment):
    """Live, recently visited panels get 3/4 of durable queue claims."""
    day = moment.astimezone(SHANGHAI).date().isoformat()
    codes = ReadCache.params['detail_codes']
    single_stock = case((func.jsonb_typeof(codes) == 'array', func.jsonb_array_length(codes)), else_=0) == 1
    return and_(ReadCache.accessed_at >= moment - INTERACTIVE_RECENCY,
        or_(ReadCache.params['day'].astext.is_(None), ReadCache.params['day'].astext == day),
        ReadCache.params['as_of_time'].astext.is_(None),
        or_(ReadCache.operation == 'workbench', and_(ReadCache.operation == 'review-overview',
            ReadCache.params['trade_day'].astext == day), and_(ReadCache.operation == 'details',
            or_(single_stock, and_(ReadCache.params['sector'].astext.is_not(None),
                                   ReadCache.params['sector'].astext != '')))))


def run_one(db, *, lane='all'):
    if lane not in {'all', 'focused', 'other'}:
        raise ValueError('unknown_read_lane')
    moment = datetime.now(UTC)
    dispatch = None
    if lane != 'focused':
        db.execute(delete(ReadCache).where(ReadCache.accessed_at < moment - RETENTION,
            or_(ReadCache.lease_until.is_(None), ReadCache.lease_until <= moment)))
        db.commit()
        # The ordinary lane retains the durable quota. A focused claim uses
        # only its own row lock, so retention or this scheduling row cannot
        # delay a newly visible panel. Compute holds neither scheduling lock.
        db.execute(insert(ReadCacheDispatch).values(id=1, turn=0).on_conflict_do_nothing(index_elements=['id']))
        dispatch = db.scalar(select(ReadCacheDispatch).where(ReadCacheDispatch.id == 1).with_for_update())
    eligible = select(ReadCache).where(ReadCache.accessed_at >= moment - RETENTION,
        ReadCache.operation.not_in(['quote-focus', 'kpl-focus']),
        ReadCache.refresh_after <= func.clock_timestamp(), or_(ReadCache.dirty.is_(True), ReadCache.payload.is_not(None)),
        or_(ReadCache.lease_until.is_(None), ReadCache.lease_until <= func.clock_timestamp()))
    interactive = interactive_condition(moment)
    focused = and_(interactive, ReadCache.focused_at.is_not(None),
                   ReadCache.focused_at >= moment - FOCUS_RECENCY)
    if lane == 'focused':
        order = (focused,)
    elif lane == 'other':
        eligible = eligible.where(not_(focused))
        preferred = interactive if dispatch.turn < 3 else not_(interactive)
        order = (preferred, not_(preferred))
    elif db.scalar(eligible.with_only_columns(ReadCache.key).where(focused).limit(1)) is not None:
        # Half the claims serve visible panels, a quarter other live panels,
        # and a quarter background details. Oldest attempts rotate within each
        # group, including multiple tabs; repeated focus never resets that age.
        groups = (focused, and_(interactive, not_(focused)), not_(interactive))
        preferred = (0, 1, 0, 2)[dispatch.turn]
        order = [groups[preferred]] + [group for i, group in enumerate(groups) if i != preferred]
    else:
        preferred = interactive if dispatch.turn < 3 else not_(interactive)
        order = (preferred, not_(preferred))
    # Empty groups lend their slot to the other group. Within each group the
    # oldest persisted attempt still bounds service despite repeated hot reads.
    entry = None
    for group in order:
        entry = db.scalar(eligible.where(group).options(defer(ReadCache.payload))
            .order_by(ReadCache.last_attempt_at.asc().nulls_first(), ReadCache.refresh_after, ReadCache.key)
            .limit(1).with_for_update(skip_locked=True))
        if entry is not None:
            break
    if not entry:
        db.rollback()
        return False
    key, token = entry.key, uuid.uuid4()
    entry.lease_token, entry.lease_until = token, func.clock_timestamp() + LEASE
    entry.last_attempt_at = func.clock_timestamp()
    if dispatch is not None:
        dispatch.turn = (dispatch.turn + 1) % 4
    db.commit()
    build(db, key, token)
    return True


def prepare_current_workbenches(db, *, limit=16):
    """Prepare enabled accounts without requiring a browser/login to be open.

    Accounts with the oldest prepared view rotate first. Each result still
    uses its own live authorization scope, watchlist and normal build lease.
    No supplier request or research job is enqueued here.
    """
    last = select(func.max(ReadCache.accessed_at)).where(ReadCache.owner_id == User.id,
        ReadCache.operation == 'workbench').correlate(User).scalar_subquery()
    users = list(db.scalars(select(User).where(User.enabled.is_(True))
        .order_by(last.asc().nulls_first(), User.id).limit(limit)))
    total = 0
    from app.market.postclose import default_day
    closed = default_day(db, datetime.now(UTC))
    for user in users:
        scope = cache_scope(db, user)
        if scope is None:
            continue
        for view in ('overview', 'auction', 'ladder', 'funds'):
            ensure_entry(db, user, scope, 'workbench', {'view': view}, metadata_only=True)
            total += 1
        if closed:
            ensure_entry(db, user, scope, 'review-overview',
                {'trade_day': closed.isoformat(), 'lookback': 20}, metadata_only=True)
            total += 1
            ensure_entry(db, user, scope, 'review-heights',
                {'trade_day': closed.isoformat(), 'lookback': 60}, metadata_only=True)
            total += 1
            ensure_entry(db, user, scope, 'review-new-highs',
                {'trade_day': closed.isoformat()}, metadata_only=True)
            total += 1
    return total
