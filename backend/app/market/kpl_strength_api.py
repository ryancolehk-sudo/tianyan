"""Read existing KPL board snapshots for the funds workspace, without collection."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import principal
from app.db import db_session
from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.http import envelope
from app.market.kpl_atlas_source import DATASET
from app.market.protocol import SHANGHAI
from app.market.review_api import check_cutoff
from app.market.review_store import permitted_provider

router = APIRouter()
MAX_BATCHES = 600


@router.get('/api/v1/kpl-emotion', operation_id='getKplEmotion')
def get_emotion(request: Request, trade_day: date | None = None, as_of_time: AwareDatetime | None = None,
                identity=Depends(principal), db: Session=Depends(db_session)):
    from app.market.kpl_emotion import read as read_emotion
    check_cutoff(db, as_of_time)
    return envelope(request, read_emotion(db, trade_day, as_of_time), 'partial',
        ['开盘啦原始情绪值；接收时点采样，启用前和缺失时段不回填。'], as_of_time=as_of_time)


def numeric(value):
    try:
        number = Decimal(str(value))
        return str(number) if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def limit_count(row, day, clock):
    count, stamp = row.get('limit_up_count'), row.get('limit_up_received_at')
    if not isinstance(count, str) or not count.isdigit() or not 0 <= int(count) <= 10000:
        return None, None
    try:
        receipt = datetime.fromisoformat(stamp)
        if receipt.utcoffset() is None or receipt > clock or receipt.astimezone(SHANGHAI).date() != day:
            return None, None
        return int(count), stamp
    except (ValueError, TypeError):
        return None, None


def project(records, day, clock, count_records=()):
    result = dict(trade_day=day.isoformat(), source='开盘啦', metric='板块强度', source_time=None,
        time_basis='received_at', received_at=None, state='missing', rows=[], history=[],
        coverage='missing', total_count=None, history_truncated=len(records)>MAX_BATCHES,
        scope='开盘啦原始题材强度，非涨停家数。各批为已保存分页观察，接收时间不是精确行情分钟；缺失不补零。')
    batches, counts = [], {}
    def remember(row):
        count, stamp = limit_count(row, day, clock)
        code = row.get('code')
        if code and count is not None and (code not in counts or datetime.fromisoformat(stamp) > datetime.fromisoformat(counts[code][1])):
            counts[code] = count, stamp
    for record in records[:MAX_BATCHES]:
        if record.received_at > clock or record.received_at.astimezone(SHANGHAI).date() != day:
            continue
        raw = record.payload.get('rows', [])
        if not raw or record.payload.get('observed_day') != day.isoformat():
            continue
        # Daily backfill is not an intraday observation at its import/receipt time.
        coverage = raw[0].get('coverage')
        total = raw[0].get('total_count')
        if (coverage not in {'complete_pages','partial_pages'} or not isinstance(total,str)
                or not total.isdigit() or not len(raw) <= int(total) <= 400
                or coverage == 'complete_pages' and int(total) != len(raw)):
            continue
        if any(row.get('archive_kind') != 'intraday' or row.get('source_day') != day.isoformat()
               or row.get('coverage') != coverage or row.get('total_count') != total
               or coverage == 'partial_pages' and (row.get('rank_basis') != 'sampled_strength'
                   or row.get('sampled_count') != str(len(raw))) for row in raw):
            continue
        if len({row.get('code') for row in raw}) != len(raw):
            continue
        rows = [dict(code=row['code'], name=row['name'], strength=numeric(row.get('strength')),
                     limit_up_count=limit_count(row, day, clock)[0],
                     limit_up_received_at=limit_count(row, day, clock)[1])
                for row in raw if row.get('code') and row.get('name')]
        rows.sort(key=lambda row: (row['strength'] is None, -Decimal(row['strength'] or '0'), row['code']))
        for row in raw:
            remember(row)
        stamp = record.received_at.isoformat()
        batches.append(dict(received_at=stamp, values={row['code']:row['strength'] for row in rows}))
        if result['received_at'] is None:
            result.update(rows=rows, received_at=stamp, coverage=coverage, total_count=int(total),
                          state='available' if clock-record.received_at < timedelta(minutes=10) else 'stale')
    for record in count_records:
        if record.received_at <= clock and record.payload.get('observed_day') == day.isoformat():
            for row in record.payload.get('rows', []):
                if row.get('source_day') == day.isoformat():
                    remember(row)
    for row in result['rows']:
        if row['code'] in counts:
            row['limit_up_count'], row['limit_up_received_at'] = counts[row['code']]
    result['history'] = list(reversed(batches))
    result['limit_count_coverage'] = dict(available=sum(row['limit_up_count'] is not None for row in result['rows']),
        total=len(result['rows']), time_basis='per_board_received_at')
    return result


def read(db, day=None, cutoff=None):
    clock = cutoff or now(db)
    day = day or clock.astimezone(SHANGHAI).date()
    provider = permitted_provider(db, 'direction_kpl_strength', cutoff)
    from app.market.kpl_top import read as read_top
    top=read_top(db,day,clock,cutoff=cutoff,history=False)
    if provider is None:
        return dict(project([], day, clock), state='disabled', top=top)
    records = list(db.scalars(select(EvidenceVersion).where(
        EvidenceVersion.source_id == provider.id, EvidenceVersion.kind == 'reference',
        EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.payload['dataset'].astext == DATASET,
        EvidenceVersion.payload['observed_day'].astext == day.isoformat(),
        EvidenceVersion.received_at <= clock,
    ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(MAX_BATCHES+1)))
    from app.market.kpl_limit_counts import latest
    return dict(project(records, day, clock, latest(db, provider.id, day, clock)),top=top)


@router.get('/api/v1/kpl-strength', operation_id='getKplStrength')
def get_strength(request: Request, trade_day: date | None = None, as_of_time: AwareDatetime | None = None,
                 top_only: bool = False,
                 identity=Depends(principal), db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    if top_only:
        from app.market.kpl_top import read as read_top
        clock=as_of_time or now(db)
        value=read_top(db,trade_day or clock.astimezone(SHANGHAI).date(),clock,cutoff=as_of_time)
        return envelope(request,value,'partial',
            ['独立第一页前十；仅为接收采样，不替代完整分页。'],as_of_time=as_of_time)
    return envelope(request, read(db, trade_day, as_of_time), 'partial',
                    ['强度为开盘啦原始值；涨停家数按同代码题材摘要独立续采，逐板块保留接收时间；接收采样非精确分钟行情。'], as_of_time=as_of_time)
