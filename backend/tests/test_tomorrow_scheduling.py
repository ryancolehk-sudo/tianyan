"""Calendar and restart behavior on isolated PostgreSQL; no external requests."""
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import APIError
from app.db import engine
from app.market.protocol import SHANGHAI
from app.models import Job, User
from app.tomorrow import scheduling as schedule
from app.tomorrow import service
from app.tomorrow.models import PlanRun, PlanSettings

FRIDAY = date(2026, 10, 23)
MONDAY = date(2026, 10, 26)
TUESDAY = date(2026, 10, 27)
KNOWN = [date(2026, 10, 22), FRIDAY, MONDAY, TUESDAY]


def at(day, hour=0, minute=0, second=0):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=SHANGHAI)


@pytest.fixture
def context(monkeypatch):
    clock = {'now': at(FRIDAY, 22, 31)}
    monkeypatch.setattr(schedule, 'now', lambda db: clock['now'])
    monkeypatch.setattr(service, 'now', lambda db: clock['now'])
    monkeypatch.setattr(schedule, 'calendar_days', lambda db, cutoff: KNOWN)
    monkeypatch.setattr(service, 'calendar_days', lambda db, cutoff: KNOWN)
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        owner = User(username='schedule-' + uuid.uuid4().hex, password_hash='synthetic-disabled', role='admin')
        db.add(owner)
        db.flush()
        db.add(PlanSettings(owner_id=owner.id, revision=1, enabled=True, use_ai=False,
            profile={'schedule_enabled_at': at(date(2026, 10, 22), 16).isoformat(), 'holdings': [
                {'code': '000001.SZ', 'quantity': 100, 'sellable_quantity': 100, 'cost': None, 'sector_codes': []}]},
            created_at=clock['now'] - timedelta(days=2), updated_at=clock['now']))
    return owner, clock


def running(identity):
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        row = db.get(Job, identity)
        assert row.state == 'queued'
        row.state, row.attempts = 'running', row.attempts + 1
        row.lease_token, row.lease_until = uuid.uuid4(), datetime.now(UTC) + timedelta(minutes=10)
        db.flush()
        return row


def complete_children(ids, *, failed=False):
    with Session(engine()) as db, db.begin():
        for identity in ids:
            row = db.get(Job, uuid.UUID(identity))
            row.state = 'failed' if failed else 'succeeded'
            row.error_code = 'source_unavailable' if failed else None
            row.result_ref = {'complete': not failed, 'reason': row.error_code}
            row.completed_at = datetime.now(UTC)


def slot_job(db, owner_id, slot='evening'):
    return db.scalar(select(Job).where(Job.kind == 'tomorrow.prepare', Job.owner_id == owner_id,
        Job.payload['schedule_slot'].astext == slot))


def test_slot_dedup_survives_new_scheduler_sessions_and_freezes_after_preparation(context):
    owner, clock = context
    assert schedule.schedule_plans(clock['now']) == 1
    with Session(engine()) as db:
        before = db.scalar(select(func.count()).select_from(Job))
        parent = slot_job(db, owner.id)
        identity, children = parent.id, parent.payload['child_ids']
        assert db.scalar(select(func.count()).select_from(PlanRun)) == 0
    clock['now'] += timedelta(seconds=20)
    assert schedule.schedule_plans(clock['now']) == 0
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == before
    schedule.process_prepare(running(identity))
    with Session(engine()) as db:
        assert db.get(Job, identity).state == 'queued'
        assert db.scalar(select(func.count()).select_from(PlanRun)) == 0
    complete_children(children)
    clock['now'] = at(date(2026, 10, 24), 8, 45)
    schedule.process_prepare(running(identity))
    with Session(engine()) as db:
        run = db.scalar(select(PlanRun))
        assert run.source_trade_day == FRIDAY and run.target_trade_day == MONDAY
        assert run.as_of_time == clock['now']
        assert run.preparation['scheduled_at'] == at(FRIDAY, 22, 30).isoformat()
        assert run.preparation['actual_cutoff'] == clock['now'].isoformat()
        assert any('错过原定时点' in reason for reason in run.limitations)
        assert db.get(Job, identity).state == 'succeeded'


def test_monday_morning_uses_friday_and_recovers_each_weekend_news_day():
    clock = at(MONDAY, 8, 30)
    slots = schedule.due_slots(KNOWN, clock, at(date(2026, 10, 22)))
    morning = next(row for row in slots if row['slot'] == 'overnight')
    assert morning['source'] == FRIDAY and morning['target'] == MONDAY
    cutoffs = schedule.news_cutoffs(KNOWN, clock, at(FRIDAY, 22, 30))
    assert [cut.date() for cut in cutoffs] == [FRIDAY, date(2026, 10, 24), date(2026, 10, 25), MONDAY]
    assert [cut.strftime('%H:%M:%S') for cut in cutoffs] == ['23:59:59'] * 3 + ['08:20:00']


def test_long_holiday_has_one_verified_morning_and_bounded_natural_day_recovery():
    known = [date(2026, 9, 30), date(2026, 10, 8), date(2026, 10, 9)]
    clock = at(date(2026, 10, 8), 8, 30)
    slots = schedule.due_slots(known, clock, at(date(2026, 9, 29)))
    assert len(slots) == 1 and slots[0]['slot'] == 'overnight'
    assert slots[0]['source'] == date(2026, 9, 30)
    cutoffs = schedule.news_cutoffs(known, clock, at(date(2026, 9, 30), 22, 30))
    assert len(cutoffs) == 9 and cutoffs[0].date() == date(2026, 9, 30)
    assert cutoffs[-1].date() == date(2026, 10, 8)
    very_old = schedule.news_cutoffs(known, at(date(2026, 11, 8), 8, 30), at(date(2026, 9, 30), 22, 30))
    assert len(very_old) == 30 and very_old[0].date() == date(2026, 10, 10)


def test_no_slot_before_activation_without_verified_target_or_after_target_open():
    assert schedule.due_slots(KNOWN, at(FRIDAY, 22, 40), at(FRIDAY, 22, 31)) == []
    assert schedule.due_slots([FRIDAY], at(FRIDAY, 22, 40), at(FRIDAY, 17)) == []
    assert schedule.due_slots(KNOWN, at(MONDAY, 9, 30), at(date(2026, 10, 22))) == []


def test_newer_failed_news_window_does_not_skip_the_weekend(context):
    owner, clock = context
    clock['now'] = at(MONDAY, 8, 30)
    with Session(engine()) as db, db.begin():
        for cutoff, state, complete in ((at(FRIDAY, 20), 'succeeded', True),
                (at(date(2026, 10, 25), 23, 59, 59), 'failed', False)):
            parent, _ = schedule._queue_parent(db, owner.id, 'tomorrow.news_prepare', cutoff.isoformat(),
                {'source_trade_day': cutoff.date().isoformat(), 'cutoff': cutoff.isoformat()}, clock['now'])
            parent.state, parent.result_ref = state, {'complete': complete}
    schedule.schedule_plans(clock['now'])
    with Session(engine()) as db:
        cutoffs = [datetime.fromisoformat(row.payload['cutoff']) for row in db.scalars(select(Job).where(
            Job.kind == 'tomorrow.news_prepare', Job.owner_id == owner.id))]
        assert at(date(2026, 10, 24), 23, 59, 59) in cutoffs
        assert at(FRIDAY, 23, 59, 59) in cutoffs
        assert at(MONDAY, 8, 20) in cutoffs
        assert cutoffs.count(at(date(2026, 10, 25), 23, 59, 59)) == 1


def test_daily_preparation_recovers_by_phase_and_candidate_only_does_not_repeat_core(context, monkeypatch):
    owner, clock = context
    calls = []
    def preparation(db, owner_id, source, cutoff, candidates=None, *, include_core=True):
        calls.append({'source': source, 'cutoff': cutoff, 'candidates': candidates, 'include_core': include_core})
        row = Job(owner_id=owner_id, kind='synthetic.cache.read', state='queued', attempts=0, payload={})
        db.add(row)
        db.flush()
        return {'job_ids': [str(row.id)], 'coverage': [{'state': 'queued'}], 'limitations': []}
    def bundle(db, owner_id, source, target, cutoff, *, additional_codes=()):
        assert additional_codes == ['000001.SZ']
        return {'stocks': [{'code': '600001.SH'}]}
    monkeypatch.setattr('app.tomorrow.prepare.queue_preparation', preparation)
    monkeypatch.setattr('app.tomorrow.data.build_bundle', bundle)
    with Session(engine()) as db, db.begin():
        parent = schedule._daily_preparation(db, owner.id, FRIDAY, clock['now'])
        identity = parent.id
    schedule.process_prepare(running(identity))
    with Session(engine()) as db:
        initial = db.get(Job, identity).payload
        assert initial['phase'] == 'core'
    schedule.process_prepare(running(identity))
    assert len(calls) == 1
    complete_children(initial['child_ids'])
    schedule.process_prepare(running(identity))
    assert len(calls) == 2 and calls[1]['include_core'] is False
    assert calls[1]['candidates'] == ['000001.SZ', '600001.SH']
    with Session(engine()) as db:
        candidates = db.get(Job, identity).payload
        assert candidates['phase'] == 'candidates'
    complete_children(candidates['child_ids'])
    schedule.process_prepare(running(identity))
    with Session(engine()) as db:
        final = db.get(Job, identity)
        assert final.state == 'succeeded' and final.result_ref['complete'] is True
        assert len(final.result_ref['job_ids']) == 2
    assert len(calls) == 2


def test_auxiliary_failure_finishes_preparation_and_is_attached_to_run(context):
    owner, clock = context
    assert schedule.schedule_plans(clock['now']) == 1
    with Session(engine()) as db:
        parent = slot_job(db, owner.id)
        identity, children = parent.id, parent.payload['child_ids']
    complete_children(children, failed=True)
    schedule.process_prepare(running(identity))
    with Session(engine()) as db:
        run = db.scalar(select(PlanRun))
        assert any('部分准备已终止' in reason for reason in run.limitations)
        assert all(child['error_code'] == 'source_unavailable' for child in run.preparation['children'])
        assert db.get(Job, identity).state == 'succeeded'
    assert schedule.schedule_plans(clock['now']) == 0


def test_slot_that_expires_while_waiting_cannot_freeze_post_open_evidence(context):
    owner, clock = context
    schedule.schedule_plans(clock['now'])
    with Session(engine()) as db:
        parent = slot_job(db, owner.id)
        identity = parent.id
    clock['now'] = at(MONDAY, 9, 30)
    schedule.process_prepare(running(identity))
    with Session(engine()) as db:
        assert db.get(Job, identity).state == 'cancelled'
        assert db.get(Job, identity).error_code == 'scheduled_slot_expired'
        assert db.scalar(select(func.count()).select_from(PlanRun)) == 0


def test_missing_sources_cannot_make_empty_preparation_complete(context, monkeypatch):
    owner, clock = context
    monkeypatch.setattr('app.tomorrow.prepare.queue_preparation', lambda *a, **kw: {
        'job_ids': [], 'coverage': [{'state': 'gap', 'reason': 'permission_missing'}], 'limitations': ['来源缺口']})
    monkeypatch.setattr('app.tomorrow.data.build_bundle', lambda *a, **kw: {'stocks': []})
    with Session(engine()) as db, db.begin():
        parent = schedule._daily_preparation(db, owner.id, FRIDAY, clock['now'])
        identity = parent.id
    for _ in range(3):
        schedule.process_prepare(running(identity))
    with Session(engine()) as db:
        parent = db.get(Job, identity)
        assert parent.state == 'succeeded'
        assert parent.result_ref['complete'] is False
        assert parent.result_ref['coverage'][0]['reason'] == 'permission_missing'


def test_news_parent_waits_for_source_terminal_and_preserves_partial_window(context, monkeypatch):
    owner, clock = context
    requests = []
    def queue(db, owner_id, day, cutoff):
        requests.append((day, cutoff))
        child = Job(owner_id=owner_id, kind='synthetic.news', state='queued', attempts=0, payload={})
        db.add(child)
        db.flush()
        return {'job_ids': [str(child.id)], 'coverage': [], 'limitations': []}
    monkeypatch.setattr('app.tomorrow.prepare.queue_news_window', queue)
    with Session(engine()) as db, db.begin():
        parent, _ = schedule._queue_parent(db, owner.id, 'tomorrow.news_prepare', 'synthetic-news',
            {'source_trade_day': FRIDAY.isoformat(), 'cutoff': at(FRIDAY, 22).isoformat()}, clock['now'])
        identity = parent.id
    schedule.process_prepare(running(identity))
    schedule.process_prepare(running(identity))
    assert len(requests) == 1
    with Session(engine()) as db:
        children = db.get(Job, identity).payload['child_ids']
    complete_children(children, failed=True)
    schedule.process_prepare(running(identity))
    with Session(engine()) as db:
        final = db.get(Job, identity)
        assert final.state == 'succeeded' and final.result_ref['complete'] is False
        assert '未取得的内容' in final.result_ref['limitations'][0]


def test_disabling_schedule_cancels_preparation_without_collection(context, monkeypatch):
    owner, clock = context
    def forbidden(*args, **kwargs):
        pytest.fail('disabled schedule must not collect')
    monkeypatch.setattr('app.tomorrow.prepare.queue_preparation', forbidden)
    with Session(engine()) as db, db.begin():
        parent = schedule._daily_preparation(db, owner.id, FRIDAY, clock['now'])
        identity = parent.id
        db.get(PlanSettings, owner.id).enabled = False
    schedule.process_prepare(running(identity))
    with Session(engine()) as db:
        assert db.get(Job, identity).state == 'cancelled'


def test_missing_or_other_owner_child_is_not_treated_as_completed(context):
    owner, clock = context
    with Session(engine()) as db, db.begin():
        other = User(username='other-' + uuid.uuid4().hex, password_hash='synthetic-disabled', role='researcher')
        db.add(other)
        db.flush()
        child = Job(owner_id=other.id, kind='synthetic', state='succeeded', attempts=0, payload={})
        db.add(child)
        db.flush()
        for identity in (str(child.id), str(uuid.uuid4())):
            with pytest.raises(APIError, match='准备子任务缺失'):
                schedule._children(db, owner.id, [identity])
