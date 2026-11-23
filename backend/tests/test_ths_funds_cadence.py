"""Full-day receipt history survives 30-second polling; offline PostgreSQL."""
from datetime import datetime,timedelta
from sqlalchemy.orm import Session
from app.db import engine
from app.models import Provider
from app.evidence.schemas import ReferencePayload
from app.market.collect import store_reference_batch
from app.market.reference import ReferenceBatch
from app.market.models import SourceSettings
from app.market.service import source_id
from app.market.ths_funds_api import read
from test_data_sources import login,configure,configuration

OPENING=datetime.fromisoformat('2026-11-10T09:15:00+08:00')


def test_full_700_sample_day_keeps_early_points_and_cutoff_then_flags_extra_manual_history(client,monkeypatch):
    headers=login(client)
    monkeypatch.setattr('app.market.service.now',lambda db:OPENING-timedelta(days=1))
    configure(client,headers,{**configuration('ths_industry_funds'),'interval_seconds':30})
    source=source_id('ths_industry_funds')
    def append(db,provider,config,index):
        stamp=OPENING+timedelta(seconds=index*30)
        row=dict(code='881001',name='合成资金行业',net_cny=str(index),pct_change='0',company_count='1',
            received_at=stamp.isoformat(),pages='1',complete='1',missing_pages='',rejected_rows='0')
        payload=ReferencePayload(dataset='market.ths.hyzjl',title='合成资金采样',source_url='https://data.10jqka.com.cn/funds/hyzjl/',
            attribution='Synthetic only',scope='Synthetic receipt clocks, not upstream market evidence',
            columns=[dict(key=key,label=key) for key in row],rows=[row],observed_day=None)
        store_reference_batch(db,provider,config,ReferenceBatch(records=[('market.ths.hyzjl',payload,None)],expected=1,requests=0),stamp)
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source);config=db.get(SourceSettings,source)
        for index in range(700):append(db,provider,config,index)
    end=OPENING+timedelta(seconds=699*30)
    with Session(engine()) as db:
        result=read(db,'industry',OPENING.date(),end)
        assert len(result['history'])==700 and not result['history_truncated']
        assert datetime.fromisoformat(result['history'][0]['received_at'])==OPENING
        assert result['history'][0]['points']==[dict(code='881001',net_cny='0',received_at=OPENING.isoformat())]
        assert result['source_time'] is None and result['time_basis']=='received_at'
        cutoff=OPENING+timedelta(seconds=100*30)
        early=read(db,'industry',OPENING.date(),cutoff)
        assert len(early['history'])==101 and datetime.fromisoformat(early['history'][-1]['received_at'])==cutoff
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source);config=db.get(SourceSettings,source)
        for index in range(700,905):append(db,provider,config,index)
    with Session(engine()) as db:
        extended=read(db,'industry',OPENING.date(),OPENING+timedelta(seconds=904*30))
        assert len(extended['history'])==900 and extended['history_truncated']
        assert len(read(db,'industry',OPENING.date(),end)['history'])==700
    with Session(engine()) as db,db.begin():db.get(Provider,source).rights={'display':'denied','cache':'denied'}
    with Session(engine()) as db:
        assert read(db,'industry',OPENING.date(),end)['status']=='disabled'
        assert not read(db,'industry',OPENING.date(),end)['history']
