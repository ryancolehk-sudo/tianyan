import argparse
import hashlib
import logging
import signal
import socket
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from time import monotonic

from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import APIError, require_instance
from app.config import settings
from app.db import engine
from app.models import Job, WorkerHeartbeat
from app.maintenance_control import admission_engine, admit, paused
from app.market.connected_specs import TDX_FAMILIES

log = logging.getLogger("jiulong.worker")


ROLES = ('all', 'scheduler', 'realtime', 'recovery', 'general')
HEARTBEAT_ID = None
TDX_EXECUTION_SOURCES = tuple(str(uuid.uuid5(uuid.NAMESPACE_URL, 'jiulong-pro-max/source/' + family))
                              for family in sorted(TDX_FAMILIES))
TDX_FULL_MINUTE_SOURCE = str(uuid.uuid5(uuid.NAMESPACE_URL, 'jiulong-pro-max/source/tdx_full_minute'))
EASTMONEY_FUNDS_SOURCE = str(uuid.uuid5(uuid.NAMESPACE_URL, 'jiulong-pro-max/source/eastmoney_funds'))


def heartbeat(worker_id: str, *, progress=True, job_id=None):
    with Session(engine()) as db, db.begin():
        values = {'seen_at': func.now()}
        if progress:
            values.update(progress_at=func.now(), job_id=job_id)
        stmt = insert(WorkerHeartbeat).values(worker_id=worker_id, **values)
        db.execute(stmt.on_conflict_do_update(index_elements=[WorkerHeartbeat.worker_id], set_=values))


def record_page_progress(job, worker_id, detail):
    """Publish business progress only while this exact lease still owns the job."""
    from app.market.protocol import SourceFailure
    with Session(engine()) as db, db.begin():
        current = db.scalar(select(Job).where(Job.id == job.id, Job.state == 'running',
            Job.lease_token == job.lease_token, Job.lease_until > func.now()).with_for_update())
        if current is None:
            raise SourceFailure('lease_expired')
        current.result_ref = dict(current.result_ref or {}, funds_page_progress=dict(detail,
            completed_at=db.scalar(select(func.clock_timestamp())).isoformat()))
        values = dict(seen_at=func.now(), progress_at=func.now(), job_id=job.id)
        stmt = insert(WorkerHeartbeat).values(worker_id=worker_id, **values)
        db.execute(stmt.on_conflict_do_update(index_elements=[WorkerHeartbeat.worker_id], set_=values))


def scheduled_bulk_funds():
    return and_(Job.kind == 'source.collect', Job.payload['source_id'].astext == EASTMONEY_FUNDS_SOURCE,
                Job.payload['scheduled_eastmoney'].astext == 'true')


def closing_review(clock=None):
    """Today's bounded closing tables use the idle general slot, not old backfills."""
    local = func.timezone('Asia/Shanghai', clock if clock is not None else func.clock_timestamp())
    return and_(Job.kind == 'review.collect',
        Job.payload['review_context']['dataset'].astext.in_(
            ['daily', 'limits', 'pools', 'group_basics', 'pool_details',
             'stock_funds', 'market_funds', 'emotion_history']),
        Job.payload['review_context']['scope'].astext == 'all',
        Job.payload['review_context']['day'].astext == func.to_char(local, 'YYYY-MM-DD'),
        func.to_char(local, 'HH24:MI:SS') >= '16:30:00',
        func.to_char(local, 'HH24:MI:SS') < '21:30:00')


def lane_expression(*, clock=None):
    from app.market.realtime_schedule import FAMILIES
    from app.market.auction_schedule import WINDOWS
    live_ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, 'jiulong-pro-max/source/' + family))
                for family in FAMILIES | set(WINDOWS) | {'tushare_calendar_sse','tushare_calendar_szse'}]
    historical = or_(Job.payload['source_id'].astext == TDX_FULL_MINUTE_SOURCE,
        *(Job.payload.has_key(key) for key in (  # noqa: W601
        'minute_context', 'daily_history', 'dated_context', 'reference_context', 'full_minute_context')))
    return case(
        (and_(Job.kind=='source.collect',Job.payload['source_id'].astext==str(uuid.uuid5(
            uuid.NAMESPACE_URL,'jiulong-pro-max/source/tencent_market_rt'))),'general'),
        (and_(Job.kind == 'review.collect', Job.payload['review_context']['dataset'].astext.in_(
            ['calendar_sse','calendar_szse']), Job.payload['review_context']['day'].astext ==
            func.to_char(func.timezone('Asia/Shanghai', func.clock_timestamp()), 'YYYY-MM-DD')), 'realtime'),
        (Job.kind == 'funds.focus', 'realtime'),
        # Two bounded native daily reads and an explicit connection probe must
        # not wait behind the multi-day recovery backlog. Historical new-high
        # members still use recovery, and the final permit rechecks the day.
        (and_(Job.kind == 'review.collect', Job.payload['review_context']['dataset'].astext.in_(
            ['new_highs','new_high_history']), or_(Job.payload['review_latest'].astext == 'true',
            Job.payload['integration_context']['probe'].astext == 'true')), 'general'),
        # Same source leases and physical request budgets still serialize the
        # reads. Historical/large membership archives keep their recovery lane.
        (closing_review(clock), 'general'),
        (Job.kind.in_(['funds.archive', 'direction.archive', 'breadth.archive', 'review.collect']), 'recovery'),
        # An automatic full-market sweep may hold a slot for several minutes.
        (scheduled_bulk_funds(), 'recovery'),
        (and_(Job.kind == 'source.collect', historical), 'recovery'),
        (and_(Job.kind == 'source.collect', Job.payload['source_id'].astext.in_(live_ids)), 'realtime'),
        else_='general')


def claim_deadline(lane):
    """Persistent due time plus a bounded head start, never absolute priority."""
    from app.market.realtime_schedule import service_grace
    families = ('tencent_web','direction_kpl_top','duanxianxia_auction', 'tushare_calendar_sse', 'tushare_calendar_szse',
                'eastmoney_funds', 'eastmoney_boards', 'mairui_funds')
    grace = case(*[(Job.payload['source_id'].astext == str(uuid.uuid5(uuid.NAMESPACE_URL,
        'jiulong-pro-max/source/' + family)), timedelta(seconds=service_grace(family)))
        for family in families], else_=timedelta(seconds=15))
    return Job.available_at + case((lane == 'realtime', grace), else_=timedelta())


def source_claim_allowed(db, job, clock):
    """Serialize claim decisions across slots, then respect another valid lease.

    A transaction lock closes the race between queued jobs for one source.
    Native TdxAiData families share a single execution group across live and
    recovery lanes, so two slow SDK subprocesses cannot occupy both live slots.
    Other HTTP sources retain source-level concurrency. The lock is released
    at commit; valid running leases and the exact write token remain the guard.
    """
    source = job.payload.get('source_id')
    if not source:
        return True
    source = str(source)
    native_tdx = source in TDX_EXECUTION_SOURCES
    namespace = 'provider:execution:tdx-native' if native_tdx else 'source:execution:' + source
    scope = (Job.payload['source_id'].astext.in_(TDX_EXECUTION_SOURCES) if native_tdx else
             Job.payload['source_id'].astext == source)
    key = int.from_bytes(hashlib.sha256(namespace.encode()).digest()[:8],
                         'big', signed=True)
    if not db.scalar(select(func.pg_try_advisory_xact_lock(key))):
        return False
    active = db.scalar(select(Job.id).where(Job.id != job.id,
        scope, Job.state == 'running', Job.lease_until > clock).limit(1))
    if active is not None:
        return False
    if job.kind == 'direction.archive':
        if any(job.payload.get(flag) is True for flag in ('kpl_counts', 'kpl_leaders')):
            from app.market.kpl_count_jobs import live_priority_deadline
            try: detail_source = uuid.UUID(source)
            except ValueError: return False
            if live_priority_deadline(db, detail_source, clock) is not None:
                return False
        elif source == str(uuid.uuid5(uuid.NAMESPACE_URL, 'jiulong-pro-max/source/direction_kpl_strength')):
            from app.market.direction_archive import in_window as archive_window
            from app.market.sector_atlas_backfill import in_window as backfill_window
            # Keep long historical pages/repairs queued throughout the existing
            # live window. The execution-time _permit still rechecks every page.
            if not (backfill_window(clock) if job.payload.get('sector_atlas_backfill') is True else archive_window(clock)):
                return False
    if db.scalar(select(lane_expression()).where(Job.id == job.id)) == 'recovery':
        # Separate workers race for the same source lock. Sorting each worker's
        # own queue cannot stop a history backlog from repeatedly winning it.
        # Yield the next lease to a due live poll, while future retries do not
        # prevent useful recovery work between live collection windows.
        from app.market.realtime_schedule import MAX_LIVE_ATTEMPTS
        live_waiting = db.scalar(select(Job.id).where(Job.id != job.id, scope,
            lane_expression() == 'realtime', Job.attempts < MAX_LIVE_ATTEMPTS,
            or_(and_(Job.state == 'queued', Job.available_at <= clock),
                and_(Job.state == 'running', Job.lease_until < clock))).limit(1))
        if live_waiting is not None:
            return False
    return True


def quote_job():
    from app.market.cadence_policy import QUOTE_FAMILIES
    sources=[str(uuid.uuid5(uuid.NAMESPACE_URL,'jiulong-pro-max/source/'+family)) for family in QUOTE_FAMILIES-{'tencent_market_rt'}]
    return and_(Job.kind=='source.collect',Job.payload['source_id'].astext.in_(sources))


def fast_feed_job():
    from app.market.cadence_policy import FAST_FEED_FAMILIES
    from app.market.models import SourceSettings
    # Whole-market requests keep the other realtime slot's existing deadlines.
    # They must not occupy the slot reserved for focused quotes and one-page reads.
    sources=[str(uuid.uuid5(uuid.NAMESPACE_URL,'jiulong-pro-max/source/'+family))
             for family in FAST_FEED_FAMILIES-{'tushare_market_rt'}]
    fuyao=uuid.uuid5(uuid.NAMESPACE_URL,'jiulong-pro-max/source/fuyao_quotes')
    full_fuyao=and_(Job.payload['source_id'].astext==str(fuyao),exists(select(SourceSettings.source_id).where(
        SourceSettings.source_id==fuyao,SourceSettings.config['collection_mode'].astext=='full_market')))
    return and_(Job.kind=='source.collect',Job.payload['source_id'].astext.in_(sources),~full_fuyao)


def general_live_job():
    """Only automatic bounded reads can use an otherwise idle general slot."""
    from app.market.collection_cadence import HISTORY_KEYS
    sources = [str(uuid.uuid5(uuid.NAMESPACE_URL, 'jiulong-pro-max/source/' + family))
               for family in ('tushare_market_rt', 'ths_industry_funds')]
    return and_(Job.kind == 'source.collect', Job.payload['source_id'].astext.in_(sources),
        Job.payload['scheduled_realtime'].astext == 'true',
        Job.payload['schema_probe'].astext.is_distinct_from('true'),
        Job.payload['integration_context']['probe'].astext.is_distinct_from('true'),
        ~or_(*(Job.payload.has_key(key) for key in HISTORY_KEYS)))  # noqa: W601


def claim(role='all', *, quotes_only=False, fast_only=False, prefer_background=False, assist_live=False):
    if assist_live and role != 'general':
        raise ValueError('Live assistance is only available to the general slot')
    if (quotes_only or fast_only) and role!='realtime':
        raise ValueError('The quote queue is only available to a realtime slot')
    if role not in ROLES or role == 'scheduler':
        if role == 'scheduler':
            return None
        raise ValueError('unknown worker role')
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        require_instance(db)
        if paused(db):
            return None
        admit(db)
        now = db.scalar(select(func.clock_timestamp()))
        # Expired final attempts are terminal; they must not remain running forever.
        lane = lane_expression()
        role_filter = True if role == 'all' else lane == role
        if assist_live:
            role_filter = or_(role_filter, and_(lane == 'realtime', general_live_job()))
        if quotes_only:
            role_filter=and_(role_filter,quote_job())
        if fast_only:
            role_filter=and_(role_filter,fast_feed_job())
        from app.market.realtime_schedule import MAX_LIVE_ATTEMPTS
        live_collection = and_(Job.kind == 'source.collect', or_(lane == 'realtime', scheduled_bulk_funds(),
            Job.payload['source_id'].astext==str(uuid.uuid5(uuid.NAMESPACE_URL,'jiulong-pro-max/source/tencent_market_rt'))))
        attempt_limit = case((live_collection, MAX_LIVE_ATTEMPTS), else_=3)
        exhausted = db.scalars(select(Job).where(role_filter, Job.kind.not_like('ai.%'), Job.kind != 'news.ai', Job.state == "running", Job.lease_until < now, Job.attempts >= attempt_limit)
                               .with_for_update(skip_locked=True)).all()
        for failed in exhausted:
            failed.state, failed.error_code, failed.completed_at = "failed", "retry_exhausted", now
            failed.lease_token = failed.lease_until = None
            if failed.kind == "snapshot.create":
                from app.evidence.models import Snapshot
                snapshot = db.scalar(select(Snapshot).where(Snapshot.id == failed.payload.get("snapshot_id"), Snapshot.owner_id == failed.owner_id).with_for_update())
                if snapshot and snapshot.state == "pending":
                    snapshot.state, snapshot.error_code = "failed", "retry_exhausted"
        review = Job.kind == 'review.collect'
        dataset = Job.payload['review_context']['dataset'].astext
        # A bulk historical batch must not hold up ordinary live/user jobs.
        # First make day statistics readable, then fetch large member/news pages.
        review_priority = case(
            (Job.kind.in_(['funds.archive','direction.archive','breadth.archive']), 5),
            (review & dataset.in_(['members']), 2),
            (review & dataset.in_(['news']), 3),
            (review & dataset.in_(['indices', 'calendar_sse', 'calendar_szse', 'tencent_minutes', 'sina_minutes']), 0),
            (review, 1), (Job.kind == 'review.prepare', 0), (Job.kind == 'screen.evaluate', 4), else_=-1)
        review_day = case((review, Job.payload['review_context']['day'].astext), else_='')
        from app.market.direction_archive import in_window as archive_window
        from app.market.sector_atlas_backfill import in_window as backfill_window
        # Filter ineligible long KPL archives before the bounded candidate scan;
        # otherwise 64 older rows can hide a ready count/leader packet forever.
        archive_ready = or_(Job.kind != 'direction.archive',
            Job.payload['source_id'].astext.is_distinct_from(str(uuid.uuid5(
                uuid.NAMESPACE_URL, 'jiulong-pro-max/source/direction_kpl_strength'))),
            Job.payload['kpl_counts'].astext == 'true', Job.payload['kpl_leaders'].astext == 'true',
            and_(Job.payload['sector_atlas_backfill'].astext == 'true', backfill_window(now)),
            and_(Job.payload['sector_atlas_backfill'].astext.is_distinct_from('true'), archive_window(now)))
        query = select(Job).where(role_filter, Job.kind.not_like('ai.%'), Job.kind != 'news.ai',
            archive_ready, Job.attempts < attempt_limit, or_(
                and_(Job.state == 'queued', Job.available_at <= now),
                and_(Job.state == 'running', Job.lease_until < now)))
        skipped = []
        job = None
        bulk_quote=and_(Job.kind=='source.collect',Job.payload['source_id'].astext==str(uuid.uuid5(
            uuid.NAMESPACE_URL,'jiulong-pro-max/source/tencent_market_rt')))
        bulk_order=case((bulk_quote,1 if prefer_background else 0),else_=0 if prefer_background else 1)
        # Existing general/user work always comes first, including the bulk
        # quote. The shared row/source locks still arbitrate with live slots.
        lane_order = case((lane == ('general' if assist_live else 'realtime'), 0), else_=1)
        # Lock one row at a time so the other slot can claim unrelated sources.
        for _ in range(64):
            candidate = db.scalar(query.where(Job.id.not_in(skipped)).order_by(
                lane_order,bulk_order, claim_deadline(lane),
                review_priority, review_day.desc(), Job.available_at,
                Job.created_at, Job.id).with_for_update(skip_locked=True).limit(1))
            if candidate is None:
                break
            if source_claim_allowed(db, candidate, now):
                job = candidate
                break
            skipped.append(candidate.id)
        if not job:
            return None
        job.state, job.lease_token = "running", uuid.uuid4()
        job.attempts += 1
        job.lease_until = now + timedelta(seconds=settings().lease_seconds)
        job.error_code = None
        return job


def renew(job_id: uuid.UUID, token: uuid.UUID) -> bool:
    with Session(engine()) as db, db.begin():
        result = db.execute(update(Job).where(Job.id == job_id, Job.state == "running", Job.lease_token == token,
                                             Job.lease_until > func.now()).values(
            lease_until=datetime.now(UTC) + timedelta(seconds=settings().lease_seconds)))
        return result.rowcount == 1


def finish(job_id: uuid.UUID, token: uuid.UUID, *, error_code: str | None = None, retry=False) -> bool:
    now = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        job = db.scalar(select(Job).where(Job.id == job_id, Job.state == "running", Job.lease_token == token,
                                         Job.lease_until > func.now()).with_for_update())
        if not job:
            return False
        job.error_code = error_code
        if error_code and retry and job.attempts < 3:
            job.state, job.completed_at = "queued", None
            job.available_at = now + timedelta(seconds=2 ** job.attempts)
        else:
            job.state = "failed" if error_code else "succeeded"
            job.completed_at = now
        job.lease_token = job.lease_until = None
        return True


@contextmanager
def lease_renewal(job):
    stop = threading.Event()
    def keep_alive():
        while not stop.wait(max(0.25, settings().lease_seconds / 3)):
            try:
                if not renew(job.id, job.lease_token):
                    return
                heartbeat(HEARTBEAT_ID or socket.gethostname(), progress=False)
            except SQLAlchemyError:
                log.error("worker lease renewal unavailable; final commit remains fenced")
    thread = threading.Thread(target=keep_alive, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)


def process(job: Job):
    if job.kind == 'funds.focus':
        from app.market.fund_focus import process_job
        with lease_renewal(job):
            process_job(job)
        return
    if job.kind in {'funds.archive', 'direction.archive', 'breadth.archive'}:
        from app.market.protocol import SourceFailure
        with lease_renewal(job):
            try:
                if job.kind == 'funds.archive':
                    from app.market.fund_history import process_job as collect_archive
                elif job.kind == 'direction.archive':
                    from app.market.direction_archive import process_job as collect_archive
                else:
                    from app.market.breadth_history import process_job as collect_archive
                collect_archive(job)
            except SourceFailure as error:
                finish(job.id, job.lease_token, error_code=error.reason)
        return
    if job.kind == 'screen.evaluate':
        from app.screening.jobs import process_screen
        with lease_renewal(job):
            process_screen(job)
        return
    if job.kind in {'tomorrow.build', 'tomorrow.prepare', 'tomorrow.news_prepare', 'tomorrow.collect'}:
        with lease_renewal(job):
            if job.kind == 'tomorrow.build':
                from app.tomorrow.jobs import process_build
                process_build(job)
            elif job.kind in {'tomorrow.prepare', 'tomorrow.news_prepare'}:
                from app.tomorrow.scheduling import process_prepare
                process_prepare(job)
            else:
                from app.market.protocol import SourceFailure
                from app.tomorrow.prepare import process_preparation
                try:
                    process_preparation(job)
                except SourceFailure as error:
                    finish(job.id, job.lease_token, error_code=error.reason)
        return
    if job.kind == 'news.ai':
        from app.news_center.jobs import process_ai
        process_ai(job)
        return

    if job.kind in {'review.collect', 'review.prepare'}:
        from app.market.protocol import SourceFailure
        from app.market.review_jobs import collect_job as collect_review
        from app.market.review_jobs import prepare_job
        with lease_renewal(job):
            try:
                (collect_review if job.kind == 'review.collect' else prepare_job)(job)
            except SourceFailure as error:
                finish(job.id, job.lease_token, error_code=error.reason)
        return
    if job.kind == 'source.collect':
        from app.market.collect import collect_job
        from app.market.protocol import SourceFailure
        with lease_renewal(job):
            try:
                collect_job(job)
            except SourceFailure as error:
                finish(job.id,job.lease_token,error_code=error.reason)
        return
    if job.kind == "snapshot.create":
        from app.evidence.service import fail_snapshot, freeze_snapshot
        with lease_renewal(job):
            try:
                freeze_snapshot(job)
            except APIError as error:
                fail_snapshot(job, error.code)
            except ValueError:
                fail_snapshot(job, "snapshot_invalid")
        return
    if job.kind != "system.check":
        finish(job.id, job.lease_token, error_code="handler_not_implemented")
        return
    with Session(engine()) as db:
        db.execute(select(func.now())).scalar_one()
    finish(job.id, job.lease_token)


def schedule_tomorrow():
    # Tomorrow's schedulers own several short business transactions. Keep one
    # independent admission transaction through all of them, so their commits
    # cannot release the updater's drain fence between stages or queue writes.
    with Session(admission_engine()) as gate, gate.begin():
        if paused(gate):
            return
        admit(gate)
        from app.tomorrow.jobs import advance_runs
        from app.tomorrow.scheduling import schedule_plans
        from app.tomorrow.verification import append_due_readbacks
        schedule_plans()
        advance_runs()
        append_due_readbacks()


def schedule_all(*, startup=False, progress=None):
    """Only the short current-source schedulers belong on the critical path."""
    from app.market.service import schedule_sources
    from app.market.resilience import reconcile as reconcile_sources
    from app.market.fund_focus import schedule as schedule_selected_funds
    last_dispatch = monotonic()
    def dispatch_sources():
        nonlocal last_dispatch
        try:
            schedule_sources()
        finally:
            last_dispatch = monotonic()
    def checkpoint():
        nonlocal last_dispatch
        if progress:
            try: progress()
            except (SQLAlchemyError, APIError):
                log.error('scheduler progress unavailable')
        if monotonic() - last_dispatch >= 2:
            run_schedulers((dispatch_sources,), progress=progress)
    def check_health():
        reconcile_sources(checkpoint=checkpoint)
    run_schedulers((dispatch_sources, check_health, schedule_selected_funds), progress=progress)


def schedule_archives(*, progress=None):
    from app.market.anomaly_archive import schedule_anomaly
    from app.market.postclose_schedule import schedule_postclose
    from app.market.review_schedule import schedule_daily_archives
    from app.market.fund_history import schedule_fund_archives
    from app.market.direction_archive import schedule_direction_archives
    from app.market.sector_atlas_backfill import schedule_atlas_backfill
    from app.market.breadth_history import schedule_breadth_archives
    run_schedulers((schedule_postclose, schedule_daily_archives, schedule_fund_archives,
                    schedule_direction_archives, schedule_atlas_backfill, schedule_breadth_archives,
                    schedule_anomaly), progress=progress)


def schedule_general(*, progress=None):
    from app.news_center.jobs import schedule_news
    from app.screening.jobs import schedule_screens
    run_schedulers((schedule_news, schedule_screens, schedule_tomorrow), progress=progress)


def run_schedulers(schedulers, *, progress=None):
    for scheduler in schedulers:
        try:
            if HEARTBEAT_ID:
                heartbeat(HEARTBEAT_ID)
            scheduler()
            if progress:
                progress()
        except (SQLAlchemyError, APIError):
            # One malformed/temporarily unavailable data family cannot prevent
            # every other family from being scheduled in the same round.
            log.error('scheduler unavailable: %s', scheduler.__name__)


def reconcile_history(*, startup=False, progress=None):
    from app.market.recovery import reconcile
    reconcile(force=startup, progress=progress)


def legacy_history_window():
    """The compatibility all loop must not spend an archive scan at opening.

    Standard installations always run a separate recovery process. The old
    all entry can maintain archives while idle, away from a live transition.
    Unknown calendars stay conservative until their ordinary source job runs.
    """
    from app.evidence.common import now
    from app.market.session import market_session
    with Session(engine()) as db:
        clock = now(db)
        session = market_session(db, clock)
        return bool(session.is_trading_day is not None and not session.automatic_live and
            session.next_transition_at and session.next_transition_at > clock + timedelta(seconds=120))


def main(argv=None):
    global HEARTBEAT_ID
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', choices=ROLES, default='all')
    parser.add_argument('--slot', choices=('primary', 'secondary'))
    parser.add_argument('--queue', choices=('all','quotes','fast'), default='all')
    args = parser.parse_args(argv)
    role = args.role
    if args.slot and role != 'realtime':
        parser.error('--slot is only supported for realtime workers')
    if args.queue!='all' and role!='realtime':
        parser.error('--queue quotes/fast is only supported for realtime workers')
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    worker_id = socket.gethostname() if role == 'all' else 'market:' + role + ':' + socket.gethostname()
    if args.slot:
        worker_id = 'market:realtime:' + args.slot + ':' + socket.gethostname()
    HEARTBEAT_ID = worker_id
    startup = True
    history_startup = True
    from time import monotonic as quote_clock
    last_quote_dispatch = float('-inf')
    yield_bulk=False
    from app.worker_watchdog import ProgressWatchdog, progress_reporting
    watchdog = ProgressWatchdog(role)
    watchdog.start()
    def scheduler_progress():
        watchdog.advance()
        heartbeat(worker_id)
    while not stop.is_set():
        job = None
        try:
            heartbeat(worker_id)
            if role in {'all', 'scheduler'}:
                schedule_all(startup=startup, progress=scheduler_progress)
                startup = False
            elif role == 'general':
                schedule_general(progress=scheduler_progress)
            elif role == 'recovery':
                schedule_archives(progress=scheduler_progress)
            if args.queue!='all' and quote_clock()-last_quote_dispatch>=.5:
                from app.market.cadence_policy import QUOTE_FAMILIES, FAST_FEED_FAMILIES
                from app.market.service import schedule_sources
                dispatch=FAST_FEED_FAMILIES if args.queue=='fast' else QUOTE_FAMILIES
                run_schedulers((lambda:schedule_sources(families=dispatch),),progress=scheduler_progress)
                last_quote_dispatch=quote_clock()
            job = (claim(role,quotes_only=True) if args.queue=='quotes' else
                claim(role,fast_only=True) if args.queue=='fast' else
                claim(role,assist_live=True,**({'prefer_background':True} if yield_bulk else {})) if role=='general' else
                claim(role,prefer_background=True) if yield_bulk else claim(role))
            if job:
                heartbeat(worker_id, job_id=job.id)
                def page_progress(detail):
                    record_page_progress(job, worker_id, detail)
                    watchdog.advance()
                with progress_reporting(page_progress):
                    process(job)
                yield_bulk=job.kind=='source.collect' and job.payload.get('source_id')==str(uuid.uuid5(
                    uuid.NAMESPACE_URL,'jiulong-pro-max/source/tencent_market_rt'))
            if role == 'all' and job is None:
                schedule_general(progress=scheduler_progress)
                schedule_archives(progress=scheduler_progress)
            # A queued archive gets a turn before scanning more coverage. The
            # reconciler has its own persisted 300-second gate and source
            # rotation; it cannot delay the scheduler or either realtime slot.
            if role == 'recovery' or (role == 'all' and job is None and legacy_history_window()):
                reconcile_history(startup=history_startup, progress=scheduler_progress)
                history_startup = False
        except (SQLAlchemyError, APIError):
            log.error("worker database unavailable; outstanding leases will recover")
        watchdog.advance()
        # An already queued batch needs no extra two-second sleep after every
        # successful task; idle polling remains bounded and supplier permits
        # still control every physical request.
        idle_poll = min(settings().poll_seconds,.25) if args.queue!='all' else settings().poll_seconds
        stop.wait(0 if job else idle_poll)
    watchdog.close()


if __name__ == "__main__":
    main()
