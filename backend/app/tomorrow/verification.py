"""Append target-day facts without pretending daily bars verify opening conditions."""
import time as monotonic_time
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import content_hash, lock, now
from app.market.protocol import SHANGHAI
from app.market.review_store import load_dataset
from app.models import User
from app.tomorrow.models import PlanConfirmation, PlanReport, PlanRun, PlanValidation
from app.tomorrow.service import permitted

_last_check = 0


def fact_readback(scenario, daily, limits, pools, coverage):
    """A factual close readback is explicitly insufficient for a qualitative open check."""
    code = scenario.get('object_id')
    quote, cap, pool = daily.get(code) or {}, limits.get(code) or {}, pools.get(code) or {}
    return dict(status='insufficient_data', comment='已追加目标日收盘事实；日线不能代替竞价、盘口承接及人工条件确认。',
        facts=dict(code=code, close=quote.get('close'), pct_chg=quote.get('pct_chg'),
                   up_limit=cap.get('up_limit'), down_limit=cap.get('down_limit'),
                   supplier_pool=pool.get('limit'), limit_times=pool.get('limit_times')),
        coverage=coverage, verification_method='rule_readback',
        required_manual_checks=scenario.get('next_checks') or [],
        limitation='未实现并获确认的机器谓词保持数据不足；缺少盘口或人工观察不视为条件满足。')


def append_readback(db, report_id, current):
    row = db.execute(select(PlanReport.id, PlanReport.owner_id, PlanReport.content_hash,
        PlanReport.payload['dossier']['scenarios'].label('scenarios'),
        PlanReport.payload['evidence_refs'].label('refs'), PlanRun.target_trade_day, PlanRun.as_of_time)
        .join(PlanRun, PlanRun.id == PlanReport.run_id)
        .join(PlanConfirmation, PlanConfirmation.report_id == PlanReport.id)
        .where(PlanReport.id == report_id)).mappings().one_or_none()
    if row is None or row['target_trade_day'] is None:
        return 0
    if current < datetime.combine(row['target_trade_day'], time(17), SHANGHAI):
        return 0
    owner = db.get(User, row['owner_id'])
    if not owner or not owner.enabled or owner.role not in {'admin', 'researcher'}:
        return 0
    original = row['refs'] or []
    visible = permitted(db, owner.id, [ref['id'] for ref in original], current)
    if any(ref['id'] not in visible or visible[ref['id']][0].content_hash != ref['content_hash'] for ref in original):
        return 0
    metadata = {key: load_dataset(db, key, row['target_trade_day'], as_of_time=current, include_rows=False)
                for key in ('daily', 'limits', 'pools')}
    references = {ref['id']: ref for table in metadata.values() for ref in table.get('evidence_refs') or []}
    valid = permitted(db, owner.id, list(references), current)
    references = {key: ref for key, ref in references.items() if key in valid
                  and valid[key][0].content_hash == ref['content_hash'] and valid[key][0].received_at > row['as_of_time']}
    coverage = {key: table.get('state') for key, table in metadata.items()}
    batch_key = content_hash(dict(report=str(report_id), original=row['content_hash'],
        references={key: ref['content_hash'] for key, ref in references.items()}, coverage=coverage))
    lock(db, 'tomorrow:readback:' + batch_key)
    if db.scalar(select(PlanValidation.id).where(PlanValidation.report_id == report_id,
        PlanValidation.observation['batch_key'].astext == batch_key).limit(1)):
        return 0
    tables = {}
    for key, table in metadata.items():
        if table.get('state') == 'complete' and all(ref['id'] in references for ref in table.get('evidence_refs') or []):
            loaded = load_dataset(db, key, row['target_trade_day'], as_of_time=current)
            tables[key] = {record.get('ts_code'): record for record in loaded.get('rows') or []}
        else:
            tables[key] = {}
    count = 0
    for scenario in row['scenarios'] or []:
        result = fact_readback(scenario, tables['daily'], tables['limits'], tables['pools'], coverage)
        db.add(PlanValidation(owner_id=owner.id, report_id=report_id, scenario_id=scenario['id'],
            status=result.pop('status'), comment=result.pop('comment'), evidence_refs=sorted(references),
            observation=dict(result, method='rule_readback', original_content_hash=row['content_hash'],
                batch_key=batch_key, checked_at=current.isoformat(), target_trade_day=str(row['target_trade_day'])),
            created_at=current))
        count += 1
    return count


def append_due_readbacks():
    global _last_check
    clock = monotonic_time.monotonic()
    if clock - _last_check < 60:
        return 0
    _last_check = clock
    with Session(engine()) as db, db.begin():
        current = now(db)
        local = current.astimezone(SHANGHAI)
        report_ids = db.scalars(select(PlanReport.id).join(PlanRun, PlanRun.id == PlanReport.run_id)
            .join(PlanConfirmation, PlanConfirmation.report_id == PlanReport.id)
            .where(PlanRun.target_trade_day.between(local.date() - timedelta(days=30), local.date()))
            .order_by(PlanConfirmation.created_at.desc()).limit(30)).all()
        return sum(append_readback(db, identity, current) for identity in report_ids)
