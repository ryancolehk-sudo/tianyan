"""Authenticated, bounded notifications for already-published local projections."""
import asyncio
import json
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.auth import APIError, principal
from app.config import settings
from app.db import db_session, engine
from app.read_cache import FOCUS_RECENCY, cache_scope, fingerprint
from app.read_cache_models import ReadCache

router = APIRouter()


def read_path(operation, params):
    """Canonical same-origin read identity; no market values in notifications."""
    values = dict(params)
    if operation == 'details':
        values.pop('view', None)
        codes = values.pop('detail_codes', [])
        if codes:
            values['codes'] = ','.join(codes)
    route = 'workbench-details' if operation == 'details' else 'workbench-view'
    return '/api/v1/' + route + '?' + urlencode(sorted(values.items()))


def published(request, token, expected_scope):
    """Recheck the session AND current rights; no long-lived ORM transaction."""
    from app.read_cache import cache_key
    with Session(engine()) as db:
        user, _ = principal(request, token=token, db=db)
        scope = cache_scope(db, user)
        if scope != expected_scope:
            raise APIError(403, 'scope_changed', '读取权限已变更，请重新读取。')
        moment = datetime.now(UTC)
        today = moment.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        rows = db.execute(select(ReadCache.key, ReadCache.operation, ReadCache.params, ReadCache.revision).where(ReadCache.owner_id == user.id,
            ReadCache.scope == scope, ReadCache.operation.in_(['workbench', 'details']),
            or_(ReadCache.params['as_of_time'].astext.is_(None),ReadCache.params['as_of_time'].astext==''),
            or_(ReadCache.params['day'].astext.is_(None),ReadCache.params['day'].astext=='',ReadCache.params['day'].astext==today),
            # Completed pinned contexts no longer advance with current quotes.
            # Only recently focused selections need their publication notice;
            # retired/background contexts must not flood the bounded stream.
            or_(ReadCache.params['observation_cutoff'].astext.is_(None),ReadCache.params['observation_cutoff'].astext=='',
                ReadCache.focused_at >= moment - FOCUS_RECENCY),
            ReadCache.payload.is_not(None),ReadCache.revision.is_not(None),ReadCache.revision!='').order_by(ReadCache.key)).all()
        # Historical cutoffs never subscribe to current-market updates.
        visible = [row for row in rows if not row.params.get('as_of_time')
            and (not row.params.get('day') or row.params['day'] == today)]
        selected={}
        for row in visible:
            path=read_path(row.operation,row.params)
            prior=selected.get(path)
            # A completed current identity wins over retained older formats,
            # regardless of hash order. Until it is built, keep the prior
            # publication available with its own unchanged revision.
            if (prior is None or row.key==cache_key(scope,row.operation,row.params)
                    or prior.key!=cache_key(scope,prior.operation,prior.params)):
                selected[path]=row
        visible=sorted(selected.values(),key=lambda row:row.key)
        versions = [(row.key, row.operation, row.params.get('view'), row.revision) for row in visible]
        return {'scope': scope, 'revision': fingerprint(versions),
                'entries': {read_path(row.operation, row.params): row.revision for row in visible},
                'views': sorted({view for _, operation, view, _ in versions if operation == 'workbench' and view})}


def frame(event, value):
    return 'event: '+event+'\ndata: '+json.dumps(value, separators=(',', ':'))+'\n\n'


async def notifications(request, token, owner, scope):
    # A dedicated LISTEN connection cannot exhaust the API's SQLAlchemy pool.
    # The browser falls back to conditional GETs if this channel is unavailable.
    url = make_url(settings().database_url).set(drivername='postgresql')
    connection = None
    try:
        connection = await psycopg.AsyncConnection.connect(url.render_as_string(hide_password=False),
            autocommit=True, connect_timeout=5)
        await connection.execute('LISTEN tianyan_workbench')
        previous = None
        entries = {}
        heartbeat = time.monotonic()
        while not await request.is_disconnected():
            try:
                state = await asyncio.to_thread(published, request, token, scope)
            except APIError as exc:
                yield frame('permission-denied', {'status': exc.status})
                return
            if state['revision'] != previous:
                previous = state['revision']
                publications = [{'path': path, 'revision': revision} for path, revision in state['entries'].items()
                                if entries.get(path) != revision]
                entries = state['entries']
                yield frame('update', {key: value for key, value in state.items() if key != 'entries'} |
                    {'publications': publications if len(publications) <= 256 else None})
            if time.monotonic() - heartbeat >= 10:
                heartbeat = time.monotonic()
                yield ': keepalive\n\n'
            async for notification in connection.notifies(timeout=1, stop_after=1):
                # Even foreign notifications contain no values. Session and
                # scope validation above governs every outgoing event.
                try:
                    value = json.loads(notification.payload)
                except (TypeError, ValueError):
                    continue
                if value.get('owner') == owner and value.get('scope') == scope:
                    break
    except asyncio.CancelledError:
        raise
    except Exception:
        # Never log connection URLs, credentials, SQL or upstream payloads.
        yield frame('unavailable', {})
    finally:
        if connection is not None:
            await connection.close()


@router.get('/api/v1/workbench-updates', operation_id='getWorkbenchUpdates', response_class=StreamingResponse,
    responses={200: {'content': {'text/event-stream': {}}, 'description': 'Local projection version events.'},
               401: {'description': 'Authentication required.'}, 403: {'description': 'Scope changed.'}})
def workbench_updates(request: Request, identity=Depends(principal), db: Session=Depends(db_session)):
    from app.auth import COOKIE
    user = identity[0]
    scope, owner = cache_scope(db, user), str(user.id)
    token = request.cookies.get(COOKIE)
    db.rollback()
    db.close()
    return StreamingResponse(notifications(request, token, owner, scope), media_type='text/event-stream',
        headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no', 'X-Content-Type-Options': 'nosniff'})
