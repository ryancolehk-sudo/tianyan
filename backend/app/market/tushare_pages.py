"""Legacy two-page helpers retained for compatibility checks, not collection.

The active collector exclusively uses tushare.DCFundPagination: 3,000 rows per
page, at most three requests and 8,000 aggregate rows. These offset-6,000 helpers
describe the earlier v2 exchange and must not be used as the current paging or
supplier-budget strategy. Existing stored page lineage remains readable.
"""
import hashlib
import json
import math
import time
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import lock, now
from app.market.models import SourceSettings
from app.market.protocol import SourceFailure
from app.models import AuditEvent, Job, Provider


def reserve_followup(job, fingerprint):
    from app.market.budget import TUSHARE_ACTION, tushare_cooldown_until
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.service import permission_reason
    from app.market.reference_backfill import validate_context
    for attempt in range(4):
        with Session(engine()) as db, db.begin():
            lock(db, TUSHARE_ACTION)
            clock = now(db)
            current = db.get(Job, job.id)
            if current is None or current.state != 'running' or current.lease_token != job.lease_token or current.lease_until <= clock:
                raise SourceFailure('lease_lost')
            provider = db.get(Provider, UUID(current.payload['source_id']))
            config = db.get(SourceSettings, provider.id) if provider else None
            if provider is None or config is None or provider.family != 'tushare_moneyflow_dc':
                raise SourceFailure('source_not_configured')
            if current.payload != job.payload or configuration_fingerprint(provider, config) != fingerprint:
                raise SourceFailure('configuration_changed')
            if permission_reason(provider) or config.paused_reason:
                raise SourceFailure('permission_missing')
            validate_context(db, current.payload, provider, config, clock)
            last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == TUSHARE_ACTION))
            deadline = tushare_cooldown_until(db, clock)
            if last and last+timedelta(seconds=2)>clock:
                deadline = max(deadline or clock, last+timedelta(seconds=2))
            delay = max(0, (deadline-clock).total_seconds()) if deadline else 0
            if not delay:
                db.add(AuditEvent(actor_id=current.owner_id, action=TUSHARE_ACTION, resource_type='job',
                    resource_id=current.id, changed_field_names=['purchased_data_request_permit'], created_at=clock))
                return
            if delay > 2.1 or attempt == 3:
                raise SourceFailure('rate_limited', retry_after=max(2, math.ceil(delay)))
        # No database transaction or budget lock is held during the short wait.
        time.sleep(delay + .03)


def combine(first, second, requested_day):
    from app.market.tushare import table, day
    from app.market.protocol import Response
    fields, first_rows = table(first.body)
    second_fields, second_rows = table(second.body, allow_empty_columns=True)
    if not second_fields and not second_rows:
        second_fields = fields
    if fields != second_fields or len(first_rows) != 6000 or len(second_rows) >= 6000:
        raise SourceFailure('market_scope_truncated')
    rows = first_rows + second_rows
    if len(rows) > 8000:
        raise SourceFailure('market_scope_truncated')
    if any(day(row.get('trade_date')) != requested_day for row in rows):
        raise SourceFailure('backfill_day_not_returned')
    if len({row.get('ts_code') for row in rows}) != len(rows):
        raise SourceFailure('duplicate_instrument')
    lineage = ['source_page_offset','source_page_received_at','source_page_sha256']
    if any(field in fields for field in lineage):
        raise SourceFailure('schema_changed')
    marked = []
    for response, values, offset in ((first,first_rows,0),(second,second_rows,6000)):
        digest = hashlib.sha256(response.body).hexdigest()
        marked.extend([[row.get(field) for field in fields] + [str(offset),response.received_at.isoformat(),digest]
                       for row in values])
    body = json.dumps({'code':0,'data':{'fields':fields+lineage,'items':marked}}).encode()
    return Response(body,second.received_at,first.elapsed_ms+second.elapsed_ms,second.status,request_count=2)
