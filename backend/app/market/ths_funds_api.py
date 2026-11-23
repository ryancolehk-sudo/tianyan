"""Authenticated reads of saved THS samples; GET never contacts upstream."""
from datetime import datetime, time, timedelta, date
from typing import Literal
from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime
from sqlalchemy import select, func, column
from sqlalchemy.dialects.postgresql import JSONB, aggregate_order_by
from sqlalchemy.orm import Session
from app.auth import principal
from app.db import db_session
from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.http import envelope
from app.market.models import DataFetchRun
from app.market.protocol import SHANGHAI
from app.market.review_api import check_cutoff
from app.market.review_store import permitted_provider

router=APIRouter()
MAX_SAMPLES=900  # 30s industry polling across 09:15–15:05 is at most 700 batches.

def read(db,category,day=None,cutoff=None):
    clock=cutoff or now(db);day=day or clock.astimezone(SHANGHAI).date()
    family='ths_'+category+'_funds';kind='hyzjl' if category=='industry' else 'gnzjl'
    result=dict(category=category,sampled_day=day.isoformat(),source_time=None,rows=[],history=[],received_at=None,
        complete=False,missing_pages=[],rejected_rows=0,pages=None,last_attempt_at=None,last_error=None,
        source='同花顺公开资金表',metric='资金净额',time_basis='received_at',status='missing',
        history_truncated=False,cross_page_duplicates=0,recovered_pages=[])
    provider=permitted_provider(db,family,cutoff)
    if provider is None:return dict(result,status='disabled')
    start=datetime.combine(day,time(),SHANGHAI);end=min(start+timedelta(days=1),clock)
    if end<start:return result
    filters=(EvidenceVersion.source_id==provider.id,
        EvidenceVersion.kind=='reference',EvidenceVersion.visibility=='shared',EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.payload['dataset'].astext=='market.ths.'+kind,
        EvidenceVersion.received_at>=start,EvidenceVersion.received_at<=end)
    latest=db.scalar(select(EvidenceVersion).where(*filters).order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1))
    run=db.scalar(select(DataFetchRun).where(DataFetchRun.source_id==provider.id,DataFetchRun.received_at<=end,
        DataFetchRun.received_at>=start).order_by(DataFetchRun.received_at.desc()).limit(1))
    if run:result.update(last_attempt_at=run.received_at.isoformat(),last_error=run.reason)
    if latest is None:return result
    rows=latest.payload.get('rows',[])
    if not rows:return result
    meta=rows[0]
    result.update(rows=rows,received_at=latest.received_at.isoformat(),complete=meta.get('complete')=='1',
        missing_pages=[int(x) for x in meta.get('missing_pages','').split(',') if x],rejected_rows=int(meta.get('rejected_rows','0')),
        pages=int(meta.get('pages','0')),cross_page_duplicates=int(meta.get('cross_page_duplicates','0')),
        recovered_pages=[int(x) for x in meta.get('recovered_pages','').split(',') if x],
        status='available' if clock-latest.received_at<timedelta(minutes=10) else 'stale')
    # Return only the three chart fields for old receipts. Re-reading every
    # page's repeated metadata would transfer unused fields on every refresh.
    cells=func.jsonb_array_elements(EvidenceVersion.payload['rows']).table_valued(
        column('value',JSONB),with_ordinality='row_number').render_derived().alias('ths_history_rows')
    point=func.jsonb_build_object('code',cells.c.value['code'].astext,
        'net_cny',cells.c.value['net_cny'].astext,'received_at',cells.c.value['received_at'].astext)
    points=select(func.jsonb_agg(aggregate_order_by(point,cells.c.row_number))).select_from(cells).correlate(EvidenceVersion).scalar_subquery()
    records=list(db.execute(select(EvidenceVersion.received_at,points.label('points')).where(*filters)
        .order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(MAX_SAMPLES+1)))
    result['history_truncated']=len(records)>MAX_SAMPLES
    result['history']=[dict(received_at=e.received_at.isoformat(),points=e.points or []) for e in reversed(records[:MAX_SAMPLES])]
    return result

@router.get('/api/v1/ths-funds',operation_id='getThsFunds')
def get_funds(request:Request,category:Literal['industry','concept']='industry',sampled_day:date|None=None,
              as_of_time:AwareDatetime|None=None,identity=Depends(principal),db:Session=Depends(db_session)):
    check_cutoff(db,as_of_time)
    return envelope(request,read(db,category,sampled_day,as_of_time),'partial',
        ['按采集时间记录；来源未提供行情时间，不能用于精确分钟交易判断。'],as_of_time=as_of_time)
