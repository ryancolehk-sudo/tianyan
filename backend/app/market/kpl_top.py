"""Independent first-page observations; never advertised as complete KPL pages."""
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.evidence.models import EvidenceVersion
from app.evidence.schemas import ReferencePayload
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.reference import ReferenceBatch

FAMILY = 'direction_kpl_top'
DATASET = 'market.direction.kpl_top_v1'
MAX_SAMPLES = 3600  # A full session at the 5s local floor, including preopen.


def parse(body, received_at, *, expected_day):
    from app.market import direction_sources as source
    data=None
    try:
        data = source._unpack('direction_kpl_strength', body)
        rows, day, _ = source._kpl(data, received_at, expected_day,check_order=False)
        count = source._number(data['Count'], integer=True, signed=False, maximum=Decimal(400))
        if len({row['code'] for row in rows}) != len(rows):
            raise ValueError('duplicate identity')
        if len(rows) != min(30, int(count)):
            raise ValueError('KPL first page size')
        # Validate exactly what is retained: the native first ten must be
        # descending and dominate every other returned row. Tail-only rank
        # movement cannot invalidate an otherwise unchanged valid first ten.
        values=[Decimal(row['strength']) for row in rows]
        head,tail=values[:10],values[10:]
        if (any(b>a for a,b in zip(head,head[1:])) or
                head and tail and min(head)<max(tail)):
            raise ValueError('KPL strength order changed')
        selected = [{**row, 'coverage':'first_page', 'archive_kind':'intraday',
            'total_count':count, 'sample_received_at':received_at.isoformat()} for row in rows[:10]]
        if not selected:
            raise ValueError('KPL first page size')
        payload = ReferencePayload(dataset=DATASET, title='开盘啦题材前十快照',
            source_url=source.KPL_URL, attribution='开盘啦公开题材排行；本人本机研究', observed_day=day,
            scope='单独请求RealRankingInfo第一页，验证19列、原生前十强度降序且不低于本页其余项；不重排来源前十。'
                '来源仅提供日期；接收采样不作为精确行情时刻，不替代完整分页覆盖。',
            columns=[dict(key=key,label=key) for key in selected[0]], rows=selected)
        return ReferenceBatch(records=[(DATASET,payload,None)],expected=1,requests=1)
    except SourceFailure as error:
        error.direction_diagnostic=source._diagnostic('direction_kpl_strength',body,data,error)
        error.direction_diagnostic['family']=FAMILY
        raise
    except (ValueError,TypeError,KeyError,ArithmeticError,AttributeError) as error:
        failure=SourceFailure('schema_changed')
        failure.direction_diagnostic=source._diagnostic('direction_kpl_strength',body,data,error)
        failure.direction_diagnostic['family']=FAMILY
        raise failure from None


def project(records, day, clock):
    result=dict(trade_day=day.isoformat(),received_at=None,source_time=None,time_basis='received_at',
        state='missing',coverage='first_page',rows=[],history=[],total_count=None,truncated=len(records)>MAX_SAMPLES)
    points=[]
    for record in records[:MAX_SAMPLES]:
        if (record.received_at>clock or record.received_at.astimezone(SHANGHAI).date()!=day
                or record.payload.get('observed_day')!=day.isoformat()):
            continue
        raw=record.payload.get('rows',[])
        if not 1<=len(raw)<=10 or len({row.get('code') for row in raw})!=len(raw):
            continue
        if any(row.get('coverage')!='first_page' or row.get('archive_kind')!='intraday'
                or row.get('source_day')!=day.isoformat()
                for row in raw):
            continue
        try:
            total=int(raw[0]['total_count'])
            if not 1<=total<=400 or len(raw)!=min(10,total):
                continue
            if any(datetime.fromisoformat(row['sample_received_at'])!=record.received_at for row in raw):
                continue
            rows=[dict(code=row['code'],name=row['name'],strength=str(Decimal(row['strength']))) for row in raw]
            if any(not Decimal(row['strength']).is_finite() for row in rows):
                continue
        except (ValueError,KeyError,TypeError,ArithmeticError):
            continue
        stamp=record.received_at.isoformat()
        points.append(dict(received_at=stamp,values={row['code']:row['strength'] for row in rows}))
        if result['received_at'] is None:
            result.update(received_at=stamp,rows=rows,total_count=total,
                state='available' if clock-record.received_at<timedelta(seconds=60) else 'stale')
    result['history']=list(reversed(points))
    return result


def read(db, day, clock, *, cutoff=None, history=True):
    from app.market.review_store import permitted_provider
    provider=permitted_provider(db,FAMILY,cutoff)
    if provider is None:
        return dict(project([],day,clock),state='disabled')
    records=list(db.scalars(select(EvidenceVersion).where(
        EvidenceVersion.source_id==provider.id,EvidenceVersion.kind=='reference',
        EvidenceVersion.visibility=='shared',EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.payload['dataset'].astext==DATASET,
        EvidenceVersion.payload['observed_day'].astext==day.isoformat(),
        EvidenceVersion.received_at<=clock,
    ).order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(MAX_SAMPLES+1 if history else 1)))
    return project(records,day,clock)
