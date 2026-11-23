"""Small, resumable count/leader packets on the existing direction recovery lane."""
from datetime import datetime, time, timedelta
import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.common import lock, now
from app.evidence.models import EvidenceVersion
from app.integrations.credentials import assert_source_binding, job_context, resolve_source_credentials
from app.market.kpl_atlas_source import DATASET as RANKING
from app.market.kpl_limit_counts import batch, latest
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.review_store import permitted_provider
from app.models import Job, User

PACKET = 8
LEADER_PACKET = 3
LIVE_GUARD_SECONDS = 17  # One 15s HTTP timeout plus the unchanged 2s physical spacing.
ACTION = 'direction.kpl-counts.schedule'
CAPABILITY = 'sector.limit_count:'
LEADER_CAPABILITY = 'sector.leaders:'


def in_window(clock):
    local = clock.astimezone(SHANGHAI).time().replace(tzinfo=None)
    return time(9, 25) <= local <= time(11, 31) or time(13) <= local <= time(23, 30)


def leader_window(clock):
    local = clock.astimezone(SHANGHAI).time().replace(tzinfo=None)
    return time(9, 15) <= local < time(15, 5)


def live_priority_deadline(db, source, clock):
    """Yield before acquiring a long source lease or starting another detail HTTP call."""
    config = db.get(SourceSettings, source)
    if config is None or not leader_window(clock):
        return None
    limit = clock + timedelta(seconds=LIVE_GUARD_SECONDS)
    due = db.scalar(select(Job.available_at).where(Job.kind == 'source.collect',
        Job.payload['source_id'].astext == str(source), Job.state == 'queued',
        Job.available_at <= limit).order_by(Job.available_at).limit(1))
    if config.paused_reason is None and config.next_run_at <= limit:
        due = min(due, config.next_run_at) if due is not None else config.next_run_at
    return max(clock + timedelta(seconds=3), due) if due is not None else None


def schedule(*, leaders=False):
    if not settings().data_collection_enabled:
        return
    from app.maintenance_control import admit, paused
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.kpl_strength_api import project
    from app.market.session import market_session
    from app.market.kpl_atlas_budget import cooldown_until
    with Session(engine()) as db, db.begin():
        clock = now(db)
        if not (leader_window(clock) if leaders else in_window(clock)) or paused(db): return
        flag = 'kpl_leaders' if leaders else 'kpl_counts'
        capability = LEADER_CAPABILITY if leaders else CAPABILITY
        packet = LEADER_PACKET if leaders else PACKET
        admit(db); lock(db, ACTION + (':leaders' if leaders else ''))
        day = clock.astimezone(SHANGHAI).date()
        if market_session(db, clock).is_trading_day is not True: return
        provider = permitted_provider(db, 'direction_kpl_strength')
        config = db.get(SourceSettings, provider.id) if provider else None
        owner = db.get(User, config.owner_id) if config else None
        if not owner or not owner.enabled or owner.role != 'admin' or config.paused_reason not in {None, 'schema_changed'}: return
        if cooldown_until(db, provider.id, clock): return
        active = list(db.scalars(select(Job).where(Job.kind == 'direction.archive',
            Job.payload['source_id'].astext == str(provider.id),
            Job.payload[flag].astext == 'true', Job.state.in_(['queued', 'running'])).with_for_update()))
        for task in active:
            if task.state == 'queued' and task.payload.get('day') != str(day):
                task.state, task.error_code, task.completed_at = 'cancelled', 'source_date_mismatch', clock
            else:
                return
        from app.market import read_facets
        saved = db.scalar(read_facets.with_facets(select(EvidenceVersion)).where(EvidenceVersion.source_id == provider.id,
            EvidenceVersion.kind == 'reference',
            EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None),
            EvidenceVersion.received_at <= clock, read_facets.value('dataset') == RANKING,
            read_facets.observed_day() == day.isoformat(),
            EvidenceVersion.payload['rows'][0]['archive_kind'].astext == 'intraday',
        ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1))
        if saved is None: return
        rows = project([saved], day, clock, latest(db, provider.id, day, clock))['rows']
        from app.market.kpl_focus import focused_codes
        focus = focused_codes(db, owner.id, clock)
        full_top = [row['code'] for row in rows[:10]]
        from app.market.kpl_top import read as read_top
        top=read_top(db,day,clock,history=False)
        important = [row['code'] for row in top['rows']] if top['state']=='available' else []
        priority = {code:index for index,code in enumerate(dict.fromkeys(focus+important))}
        if priority:
            rows.sort(key=lambda row:priority.get(row['code'],len(priority)))
        # A final round after 15:30 catches closing-auction/late source revisions.
        local = clock.astimezone(SHANGHAI)
        threshold = datetime.combine(day, time(15, 30), SHANGHAI) if local.time() >= time(15, 30) else clock-timedelta(seconds=60)
        if leaders:
            from app.market.kpl_leaders import DATASET as LEADERS
            stamps = {}
            code = EvidenceVersion.payload['rows'][0]['plate_code'].astext
            leader_codes = list(dict.fromkeys(focus+(important or full_top)))
            for plate, stamp in db.execute(read_facets.with_facets(select(code, EvidenceVersion.received_at)).where(
                    EvidenceVersion.source_id == provider.id, EvidenceVersion.kind == 'reference', EvidenceVersion.visibility == 'shared',
                    EvidenceVersion.owner_id.is_(None), EvidenceVersion.received_at <= clock,
                    read_facets.value('dataset') == LEADERS, read_facets.observed_day() == str(day),
                    code.in_(leader_codes),
                    EvidenceVersion.payload['rows'][0]['archive_kind'].astext == 'intraday'
                    ).distinct(code).order_by(code, EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(20)):
                stamps[plate] = stamp
            rows = [{**row, 'limit_up_received_at': stamps[row['code']].isoformat() if row['code'] in stamps else None}
                for row in rows if row['code'] in leader_codes]
            threshold = clock - timedelta(seconds=max(60, config.interval_seconds))
        rows = [row for row in rows if not row['limit_up_received_at'] or datetime.fromisoformat(row['limit_up_received_at']) < threshold]
        attempts = db.scalars(select(DataFetchRun).where(DataFetchRun.source_id == provider.id,
            DataFetchRun.capability.startswith(capability), DataFetchRun.received_at > clock-timedelta(minutes=5),
            DataFetchRun.status == 'error')).all()
        cooling = {run.capability.removeprefix(capability) for run in attempts}
        rows = [row for row in rows if row['code'] not in cooling]
        if not rows: return
        def fair(values):
            missing = [row for row in values if row['limit_up_received_at'] is None]
            old = sorted((row for row in values if row['limit_up_received_at'] is not None),
                key=lambda row: (datetime.fromisoformat(row['limit_up_received_at']), row['code']))
            return missing+old if leaders else old[:3]+missing+old[3:]
        def priority_age(row):
            stamp = row['limit_up_received_at']
            return (stamp is not None, datetime.fromisoformat(stamp) if stamp else clock,
                priority.get(row['code'], len(priority)))
        order = fair(rows)
        selected = order[:packet]
        focused = sorted((row for row in rows if row['code'] in focus), key=priority_age)[:2]
        if priority:
            # Missing/oldest within each priority class must advance too when
            # a complete packet takes longer than its refresh threshold.
            top_rows = sorted((row for row in rows if row['code'] in important and row not in focused), key=priority_age)
            favored = focused if leaders else focused+top_rows[:3-len(focused)]
            ordinary = fair([row for row in rows if row['code'] not in (focus if leaders else priority)])
            selected = favored+ordinary[:packet-len(favored)]
            selected += [row for row in order if row not in selected][:packet-len(selected)]
        bound = resolve_source_credentials(db, provider.id, provider.family)
        if bound.reason: return
        payload = dict(source_id=str(provider.id), day=str(day), **{flag: True},
            codes=[dict(code=row['code'], name=row['name']) for row in selected],
            configuration_sha256=configuration_fingerprint(provider, config))
        binding = job_context(bound, provider.id)
        if binding is not None: payload['integration_context'] = binding
        db.add(Job(owner_id=owner.id, kind='direction.archive', payload=payload))


def fence(db, job):
    from app.maintenance_control import admit, paused
    from app.market.minute_backfill import configuration_fingerprint
    from app.market.session import market_session
    from app.market.registry import SOURCES
    current = db.scalar(select(Job).where(Job.id == job.id, Job.kind == 'direction.archive',
        Job.state == 'running', Job.lease_token == job.lease_token).with_for_update())
    clock = now(db)
    if current is None or current.lease_until is None or current.lease_until <= clock:
        raise SourceFailure('lease_lost')
    if not settings().data_collection_enabled: raise SourceFailure('collection_disabled')
    if paused(db): raise SourceFailure('maintenance_wait')
    admit(db)
    provider = permitted_provider(db, 'direction_kpl_strength')
    config = db.get(SourceSettings, provider.id) if provider else None
    owner = db.get(User, current.owner_id)
    if not config or not owner or not owner.enabled or owner.role != 'admin' or config.owner_id != owner.id:
        raise SourceFailure('permission_missing')
    leaders = current.payload.get('kpl_leaders') is True
    if (current.payload != job.payload or str(provider.id) != current.payload.get('source_id')
            or (current.payload.get('kpl_counts') is True) == leaders):
        raise SourceFailure('configuration_changed')
    if configuration_fingerprint(provider, config) != current.payload.get('configuration_sha256'):
        raise SourceFailure('configuration_changed')
    if config.paused_reason not in {None, 'schema_changed'}: raise SourceFailure('source_paused')
    if provider.adapter_version != SOURCES[provider.family].version: raise SourceFailure('adapter_version_mismatch')
    day = clock.astimezone(SHANGHAI).date()
    if current.payload.get('day') != str(day): raise SourceFailure('source_date_mismatch')
    if not (leader_window(clock) if leaders else in_window(clock)) or market_session(db, clock).is_trading_day is not True:
        raise SourceFailure('calendar_not_ready')
    codes = current.payload.get('codes', [])
    if (not isinstance(codes, list) or not 1 <= len(codes) <= (LEADER_PACKET if leaders else PACKET)
            or any(not isinstance(row, dict) or not re.fullmatch(r'\d{6}', str(row.get('code', '')))
                or not isinstance(row.get('name'), str) or not 1 <= len(row['name']) <= 60 for row in codes)
            or len({row['code'] for row in codes}) != len(codes)):
        raise SourceFailure('invalid_repair_scope')
    assert_source_binding(db, provider.id, provider.family, current.payload)
    return current, provider, config, day, clock


def yield_to_live(db, current, provider, clock):
    deadline = live_priority_deadline(db, provider.id, clock)
    if deadline is None:
        return False
    current.state, current.available_at = 'queued', deadline
    current.attempts = max(0, current.attempts - 1)
    current.error_code = 'live_collection_wait'
    current.lease_token = current.lease_until = None
    return True


def process_job(job):
    from app.market.collect import store_reference_batch
    from app.market.direction_sources import fetch
    from app.market.kpl_atlas_budget import reserve, record_failure
    from app.worker import finish
    leaders = job.payload.get('kpl_leaders') is True
    prefix = 'leader' if leaders else 'count'
    capability = LEADER_CAPABILITY if leaders else CAPABILITY
    with Session(engine()) as db, db.begin():
        current, _, _, _, _ = fence(db, job)
        completed = list((current.result_ref or {}).get(prefix+'_completed', []))
        failures = list((current.result_ref or {}).get(prefix+'_failed', []))
        diagnostics = list((current.result_ref or {}).get('leader_diagnostics', []))[:LEADER_PACKET] if leaders else []
    for item in job.payload.get('codes', []):
        with Session(engine()) as db, db.begin():
            current, provider, config, day, started = fence(db, job)
            if item['code'] in completed or item['code'] in failures:
                continue
            if yield_to_live(db, current, provider, started): return
        try:
            reserve(job, expected_configuration=job.payload['configuration_sha256'])
        except SourceFailure as wait:
            if wait.reason not in {'rate_limited','local_request_wait'}: raise
            with Session(engine()) as db, db.begin():
                current, _, _, _, clock = fence(db, job)
                current.state, current.available_at = 'queued', clock+timedelta(seconds=wait.retry_after or 3)
                current.attempts = max(0, current.attempts-1)
                current.error_code = 'source_budget_wait'
                current.lease_token = current.lease_until = None
            return
        # Reserving the shared permit may have waited. Recheck the deadline,
        # lease, calendar, permission and frozen configuration immediately before HTTP.
        with Session(engine()) as db, db.begin():
            current, provider, config, day, started = fence(db, job)
            if yield_to_live(db, current, provider, started): return
        response = None
        error = None
        try:
            if leaders:
                from app.market.kpl_leaders import DATASET as LEADERS, parse
                from app.market.reference import ReferenceBatch
                response = fetch('direction_kpl_strength', plate_code=item['code'])
                payload = parse(response, item['code'], day)
                parsed = ReferenceBatch(records=[(LEADERS+':'+item['code'], payload, None)], expected=1, requests=1)
            else:
                response = fetch('direction_kpl_strength', plate_code=item['code'], plate_summary=True)
                parsed = batch(response, day, item['code'], item['name'])
        except SourceFailure as failure:
            error = failure
            if failure.reason != 'retry_after_out_of_range':
                record_failure(job, failure)
        with Session(engine()) as db, db.begin():
            current, provider, config, _, clock = fence(db, job)
            if error is None:
                store_reference_batch(db, provider, config, parsed, response.received_at)
                completed.append(item['code'])
            else:
                failures.append(item['code'])
                if leaders and len(diagnostics)<LEADER_PACKET and getattr(error,'direction_diagnostic',None):
                    diagnostics.append(dict(plate_code=item['code'],**error.direction_diagnostic))
            partial = error is None and leaders and payload.rows[0]['coverage'] != 'complete'
            db.add(DataFetchRun(source_id=provider.id, job_id=job.id, capability=capability+item['code'],
                started_at=started, received_at=response.received_at if response else clock, status='error' if error else 'partial' if partial else 'complete',
                reason=error.reason if error else None, expected=1, accepted=0 if error else 1,
                rejected=1 if error or partial else 0, duplicates=0, request_count=1,
                elapsed_ms=response.elapsed_ms if response else None,
                body_hash=hashlib.sha256(response.body).hexdigest() if response else None))
            current.result_ref={prefix+'_completed':completed, prefix+'_failed':failures}
            if diagnostics:current.result_ref['leader_diagnostics']=diagnostics
            if leaders and error and error.reason == 'permission_missing':
                config.paused_reason = error.reason
        if error and error.reason == 'retry_after_out_of_range':
            # Persist this physical request before our own durable pause changes
            # the frozen configuration. No further request may follow it.
            record_failure(job, error)
        if error and error.reason not in {'schema_changed', 'source_date_mismatch',
                'upstream_unavailable', 'transport_error', 'timeout',
                'response_too_large', 'unsafe_upstream_response'}:
            raise error
    finish(job.id, job.lease_token, error_code=prefix+'_packet_partial' if failures else None)
