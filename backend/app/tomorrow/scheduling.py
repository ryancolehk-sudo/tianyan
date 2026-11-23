"""Calendar slots and durable preparation; reads never run this scheduler.

Slot time identifies intent. After preparation finishes, freeze the actual
server receipt cutoff. Vendor query windows never manufacture old receipts.
"""
import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import APIError
from app.db import engine
from app.evidence.common import content_hash, lock, now
from app.models import Job, User
from app.tomorrow.models import PlanReport, PlanRun, PlanSettings
from app.tomorrow.schemas import RunInput
from app.tomorrow.service import SHANGHAI, calendar_days, queue_run

SLOT_RECOVERY_DAYS = 7
NEWS_RECOVERY_DAYS = 30
WAIT_SECONDS = 15


def due_slots(known, clock, activated):
    """Latest eligible evening/morning intent; never guess an unknown weekday."""
    days = sorted(set(known))
    local = clock.astimezone(SHANGHAI)
    result = []
    for slot, hour, minute in (('evening', 22, 30), ('overnight', 8, 30)):
        due = [(day, datetime.combine(day, time(hour, minute), SHANGHAI)) for day in days
               if local.date() - timedelta(days=SLOT_RECOVERY_DAYS) <= day <= local.date()]
        due = [(day, cut) for day, cut in due if activated <= cut <= clock]
        if not due:
            continue
        day, scheduled = due[-1]
        source = day if slot == 'evening' else next((d for d in reversed(days) if d < day), None)
        target = next((d for d in days if source is not None and d > source), None)
        if source is None or target is None or clock >= datetime.combine(target, time(9, 30), SHANGHAI):
            continue
        result.append(dict(slot=slot, source=source, target=target, scheduled=scheduled, day=day))
    return result


def news_cutoffs(known, clock, previous=None):
    """Recover natural-day windows through holidays, bounded to thirty days."""
    local = clock.astimezone(SHANGHAI)
    end = local.replace(minute=0, second=0, microsecond=0)
    if local.hour in {8, 22} and local.minute >= 20:
        end = local.replace(minute=20, second=0, microsecond=0)
    completed = [d for d in known if d < local.date() or
                 (d == local.date() and local.time().replace(tzinfo=None) >= time(15, 5))]
    first = previous.astimezone(SHANGHAI).date() if previous else max(completed, default=local.date())
    first = max(first, local.date() - timedelta(days=NEWS_RECOVERY_DAYS - 1))
    result = []
    for offset in range(max(0, (local.date() - first).days) + 1):
        day = first + timedelta(days=offset)
        cut = end if day == local.date() else datetime.combine(day, time(23, 59, 59), SHANGHAI)
        if previous is None or cut > previous:
            result.append(cut)
    return result


def _activated(config):
    try:
        raw = config.profile.get('schedule_enabled_at')
        stamp = datetime.fromisoformat(raw) if raw else config.created_at
        return stamp if stamp.utcoffset() is not None else config.created_at
    except (ValueError, TypeError, AttributeError):
        return config.created_at


def _queue_parent(db, owner_id, kind, key, payload, clock):
    digest = content_hash(dict(owner=str(owner_id), purpose=kind, key=key))
    previous = db.scalar(select(Job).where(Job.kind == kind, Job.owner_id == owner_id,
        Job.payload['schedule_key'].astext == digest).order_by(Job.created_at).limit(1))
    if previous:
        return previous, False
    row = Job(owner_id=owner_id, kind=kind, state='queued', attempts=0,
        payload={**payload, 'schedule_key': digest, 'scheduled': True}, available_at=clock)
    db.add(row)
    db.flush()
    return row, True


def _daily_preparation(db, owner_id, source, clock):
    return _queue_parent(db, owner_id, 'tomorrow.prepare', 'daily:' + source.isoformat(),
        {'source_trade_day': source.isoformat()}, clock)[0]


def schedule_plans(current=None):
    """Return new report intents, not completed reports."""
    with Session(engine()) as db, db.begin():
        clock = current or now(db)
        local = clock.astimezone(SHANGHAI)
        lock(db, 'tomorrow:schedule')
        configurations = db.scalars(select(PlanSettings).where(PlanSettings.enabled.is_(True))).all()
        if not configurations:
            return 0
        known, made = calendar_days(db, clock), 0
        for config in configurations:
            owner = db.get(User, config.owner_id)
            if not owner or not owner.enabled or owner.role not in {'admin', 'researcher'}:
                continue
            # Existing JSONB is enough; Job has no input_hash column.
            old_news = db.scalars(select(Job).where(Job.kind == 'tomorrow.news_prepare', Job.owner_id == owner.id)).all()
            previous = []
            for job in old_news:
                if job.state != 'succeeded' or not isinstance(job.result_ref, dict) or job.result_ref.get('complete') is not True:
                    continue
                try:
                    value = datetime.fromisoformat(job.payload['cutoff'])
                    if value.utcoffset() is not None and value <= clock:
                        previous.append(value)
                except (KeyError, TypeError, ValueError):
                    pass
            # The original evening draft defines the overnight interval. Later
            # successful hourly windows must not hide an earlier missing day.
            evening = db.scalar(select(PlanRun.as_of_time).join(PlanReport, PlanReport.run_id == PlanRun.id).where(
                PlanRun.owner_id == owner.id, PlanRun.slot == 'evening', PlanRun.as_of_time <= clock,
                PlanReport.created_at <= clock).order_by(PlanRun.as_of_time.desc()).limit(1))
            anchors = ([max(previous)] if previous else []) + ([evening] if evening else [])
            news_jobs = []
            for cut in news_cutoffs(known, clock, min(anchors, default=None)):
                child, _ = _queue_parent(db, owner.id, 'tomorrow.news_prepare', cut.isoformat(),
                    {'source_trade_day': cut.astimezone(SHANGHAI).date().isoformat(), 'cutoff': cut.isoformat()}, clock)
                news_jobs.append(str(child.id))
            if local.date() in known and local.time().replace(tzinfo=None) >= time(17):
                _daily_preparation(db, owner.id, local.date(), clock)
            for due in due_slots(known, clock, _activated(config)):
                scheduled_key = f'{owner.id}:{due["day"].isoformat()}:{due["slot"]}'
                exists = db.scalar(select(PlanRun.id).where(PlanRun.owner_id == owner.id,
                    PlanRun.dedup_key == content_hash({'schedule': scheduled_key})))
                if exists:
                    continue
                base = _daily_preparation(db, owner.id, due['source'], clock)
                slot_news, _ = _queue_parent(db, owner.id, 'tomorrow.news_prepare', 'slot:' + scheduled_key,
                    {'source_trade_day': local.date().isoformat(), 'cutoff': clock.isoformat()}, clock)
                _, created = _queue_parent(db, owner.id, 'tomorrow.prepare', 'slot:' + scheduled_key,
                    {'source_trade_day': due['source'].isoformat(), 'target_trade_day': due['target'].isoformat(),
                     'phase': 'slot_wait', 'schedule_slot': due['slot'], 'scheduled_at': due['scheduled'].isoformat(),
                     'run_schedule_key': scheduled_key, 'child_ids': list(dict.fromkeys(
                         [str(base.id), *news_jobs, str(slot_news.id)]))}, clock)
                made += int(created)
        return made


def _children(db, owner_id, ids):
    try:
        identities = [uuid.UUID(value) for value in ids]
    except (ValueError, TypeError, AttributeError):
        raise APIError(422, 'preparation_context_invalid', '准备任务引用无效。') from None
    rows = list(db.scalars(select(Job).where(Job.owner_id == owner_id, Job.id.in_(identities)))) if identities else []
    if len(rows) != len(set(identities)):
        raise APIError(422, 'preparation_child_missing', '准备子任务缺失，不能冒充准备完成。')
    return rows


def _defer(current, clock):
    current.state, current.available_at = 'queued', clock + timedelta(seconds=WAIT_SECONDS)
    current.attempts = max(0, current.attempts - 1)
    current.completed_at = None
    current.lease_token = current.lease_until = None


def _combine(first, second):
    return {'job_ids': list(dict.fromkeys((first.get('job_ids') or []) + (second.get('job_ids') or []))),
            'coverage': (first.get('coverage') or []) + (second.get('coverage') or []),
            'limitations': list(dict.fromkeys((first.get('limitations') or []) + (second.get('limitations') or [])))}


def _outcomes(children):
    result = []
    for child in children:
        progress = child.result_ref if isinstance(child.result_ref, dict) else {}
        result.append({'job_id': str(child.id), 'kind': child.kind, 'state': child.state,
            'error_code': child.error_code, 'complete': progress.get('complete'), 'reason': progress.get('reason'),
            'limitations': progress.get('limitations') or [], 'coverage': progress.get('coverage') or []})
    return result


def process_prepare(job):
    from app.tomorrow.jobs import fence
    from app.tomorrow.prepare import queue_news_window, queue_preparation
    with Session(engine()) as db, db.begin():
        current, clock = fence(db, job), now(db)
        owner = db.get(User, job.owner_id)
        config = db.get(PlanSettings, job.owner_id)
        progress = dict(current.payload)
        if (not owner or not owner.enabled or owner.role not in {'admin', 'researcher'}
                or (progress.get('scheduled') and (config is None or not config.enabled))):
            current.state, current.error_code = 'cancelled', 'owner_permission_changed'
        else:
            # Expire an obsolete slot even while a source is still queued behind
            # a daily quota. Collection can continue without publishing hindsight.
            if (progress.get('phase') == 'slot_wait' and clock >= datetime.combine(
                    date.fromisoformat(progress['target_trade_day']), time(9, 30), SHANGHAI)):
                current.state, current.error_code = 'cancelled', 'scheduled_slot_expired'
                current.result_ref = {'scheduled_at': progress['scheduled_at'], 'checked_at': clock.isoformat(),
                    'limitations': ['目标交易时段已开始，本次未把事后资料补写成盘前预案。']}
                current.completed_at, current.lease_token, current.lease_until = clock, None, None
                return
            children = _children(db, owner.id, progress.get('child_ids') or [])
            if any(child.state in {'queued', 'running'} for child in children):
                _defer(current, clock)
                return
            outcomes = _outcomes(children)
            if progress.get('phase') == 'slot_wait':
                target = date.fromisoformat(progress['target_trade_day'])
                if clock >= datetime.combine(target, time(9, 30), SHANGHAI):
                    current.state, current.error_code = 'cancelled', 'scheduled_slot_expired'
                    current.result_ref = {'scheduled_at': progress['scheduled_at'], 'checked_at': clock.isoformat(),
                        'limitations': ['目标交易时段已开始，本次未把事后资料补写成盘前预案。'], 'children': outcomes}
                else:
                    view, _, _ = queue_run(db, owner, RunInput(source_trade_day=date.fromisoformat(progress['source_trade_day']),
                        target_trade_day=target, as_of_time=clock, slot=progress['schedule_slot'], use_ai=config.use_ai),
                        schedule_key=progress['run_schedule_key'])
                    run = db.get(PlanRun, uuid.UUID(view['id']))
                    run.preparation = {'scheduled_at': progress['scheduled_at'], 'actual_cutoff': clock.isoformat(),
                        'child_ids': progress.get('child_ids') or [], 'children': outcomes}
                    run.config_snapshot = {**run.config_snapshot, 'schedule': {
                        'scheduled_at': progress['scheduled_at'], 'actual_cutoff': clock.isoformat()}}
                    scheduled = datetime.fromisoformat(progress['scheduled_at'])
                    if clock - scheduled > timedelta(minutes=15):
                        run.limitations = list(dict.fromkeys(run.limitations + [
                            f'错过原定时点{scheduled.astimezone(SHANGHAI).isoformat()}后补生成；实际接收截止{clock.astimezone(SHANGHAI).isoformat()}，不冒充原时点已完成。']))
                    gaps = [value for value in outcomes if value['state'] != 'succeeded' or value['complete'] is False
                            or any(entry.get('state') == 'gap' for entry in value['coverage'])]
                    if gaps:
                        run.limitations = list(dict.fromkeys(run.limitations + [
                            '部分准备已终止但资料仍有缺口；详情保留在本次准备记录，核心不足将阻断研究。']))
                    source = date.fromisoformat(progress['source_trade_day'])
                    if (clock.astimezone(SHANGHAI).date() - source).days >= NEWS_RECOVERY_DAYS:
                        run.limitations = list(dict.fromkeys(run.limitations + [
                            '离线新闻恢复最多覆盖最近30个自然日；更早假期间隔仍需核查缓存覆盖，不能视为完整。']))
                    current.state, current.result_ref = 'succeeded', {'run_id': str(run.id),
                        'scheduled_at': progress['scheduled_at'], 'actual_cutoff': clock.isoformat(), 'children': outcomes}
            elif job.kind == 'tomorrow.news_prepare':
                if progress.get('phase') == 'news_wait':
                    result = progress.get('preparation') or {}
                    result = {**result, 'children': outcomes,
                              'complete': bool(children) and all(child.state == 'succeeded' and
                                  (child.result_ref or {}).get('complete') is True for child in children)}
                    if not result['complete']:
                        result['limitations'] = list(dict.fromkeys(result.get('limitations', []) + [
                            '资讯窗口尚未完整核验；未取得的内容不能写成没有事件。']))
                    current.state, current.result_ref = 'succeeded', result
                else:
                    result = queue_news_window(db, owner.id, date.fromisoformat(progress['source_trade_day']),
                                               datetime.fromisoformat(progress['cutoff']))
                    progress.update(phase='news_wait', child_ids=result['job_ids'], preparation=result)
                    current.payload = progress
                    _defer(current, clock)
                    return
            else:
                source_day = date.fromisoformat(progress['source_trade_day'])
                phase = progress.get('phase', 'start')
                if phase == 'start':
                    result = queue_preparation(db, owner.id, source_day, clock)
                    progress.update(phase='core', child_ids=result['job_ids'], preparation=result, core_outcomes=[])
                elif phase == 'core':
                    from app.tomorrow.data import build_bundle
                    held = [holding['code'] for holding in (config.profile.get('holdings') or [])] if config else []
                    dossier = build_bundle(db, owner.id, source_day, None, clock, additional_codes=held)
                    candidates = list(dict.fromkeys(held + [stock['code'] for stock in dossier.get('stocks') or []]))[:30]
                    result = queue_preparation(db, owner.id, source_day, clock, candidates, include_core=False)
                    progress.update(phase='candidates', child_ids=result['job_ids'],
                        preparation=_combine(progress.get('preparation') or {}, result),
                        candidates=candidates, core_outcomes=outcomes)
                elif phase == 'candidates':
                    preparation = progress.get('preparation') or {}
                    has_gaps = any(value.get('state') == 'gap' for value in preparation.get('coverage') or [])
                    current.state, current.result_ref = 'succeeded', {
                        **preparation, 'children': progress.get('core_outcomes', []) + outcomes,
                        'complete': not has_gaps and all(value['state'] == 'succeeded' and value['complete'] is not False
                            for value in progress.get('core_outcomes', []) + outcomes)}
                    current.completed_at = clock
                    current.lease_token = current.lease_until = None
                    return
                else:
                    raise APIError(422, 'preparation_context_invalid', '准备阶段无效。')
                current.payload = progress
                _defer(current, clock)
                return
        current.completed_at = clock
        current.lease_token = current.lease_until = None
