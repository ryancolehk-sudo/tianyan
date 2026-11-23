"""Full collector/store/read path on disposable PostgreSQL; synthetic HTTP only."""
from datetime import datetime as RealDateTime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.auction_book import DATASET, FAMILY
from app.market.auction_book_read import book_views
from app.market.models import SourceSettings
from app.market.protocol import InstrumentKey, Response, SHANGHAI
from app.market.service import source_id
from app.market.terminal_read import SavedTables
from app.models import Job, Provider
from app.worker import claim, process
from test_data_sources import configure, login
from test_auction_tables import new_job, configure_family, CURRENT

DAY=RealDateTime(2026,10,26,tzinfo=SHANGHAI).date()
KEYS=[InstrumentKey('SSE','600001','stock'),InstrumentKey('SZSE','000002','stock')]


def body(clock, down_volume='20'):
    rows=[]
    for key,side in zip(KEYS,('down','up')):
        f=['']*88
        values={1:'合成竞价'+side,2:key.symbol,3:'10',4:'10',9:'0',10:'0',19:'9',20:down_volume,
                30:clock.strftime('%Y%m%d%H%M%S'),47:'11',48:'9'}
        if side=='up': values.update({9:'11',10:'50',19:'0',20:'0'})
        for index,value in values.items(): f[index]=value
        rows.append('v_'+key.vendor_key+'="'+'~'.join(f)+'";')
    return '\n'.join(rows).encode('gb18030')


def setup(client,monkeypatch):
    headers=login(client)
    configure_family(client,headers,CURRENT)
    with Session(engine()) as db:
        original=db.get(SourceSettings,source_id(CURRENT)).config.copy()
    configure(client,headers,{'family':FAMILY,'symbols':[],'collection_mode':'full_market','interval_seconds':60,
        'rights_basis':'Synthetic auction-book contract fixtures, isolated database only; no vendor grant.',
        'rights':{'cache':'allowed','display':'allowed','export':'denied','redistribute':'denied'},'enabled':True,'entitlement_status':'verified'})
    monkeypatch.setattr('app.market.tdx_full_minute.universe',lambda db,config:KEYS)
    from app.config import settings
    monkeypatch.setattr(settings(),'data_collection_enabled',True)
    return headers,original


def collect(monkeypatch, clock, *, volume='20', action=None):
    class Frozen(RealDateTime):
        @classmethod
        def now(cls,tz=None): return clock.astimezone(tz) if tz else clock.replace(tzinfo=None)
    monkeypatch.setattr('app.market.collect.datetime',Frozen)
    def read(url):
        if action: action()
        return Response(body(clock.replace(second=0),volume),clock,3,200)
    monkeypatch.setattr('app.market.auction_book.fetch_https',read)
    ident=new_job(FAMILY)
    job=claim()
    assert job is not None and job.id==ident
    process(job)
    return ident


def view(clock):
    with Session(engine()) as db:
        return book_views(SavedTables(db,clock,DAY),DAY)


def evidence_count():
    with Session(engine()) as db:
        return db.scalar(select(func.count()).select_from(EvidenceVersion).where(EvidenceVersion.source_id==source_id(FAMILY)))


def test_collects_two_sides_preserves_shortline_and_does_not_fill_future_node(client,monkeypatch):
    _,original=setup(client,monkeypatch)
    clock=RealDateTime(2026,10,26,9,20,6,tzinfo=SHANGHAI)
    ident=collect(monkeypatch,clock)
    with Session(engine()) as db:
        job=db.get(Job,ident)
        assert job.state=='succeeded',job.error_code
        assert job.result_ref['up_count']==job.result_ref['down_count']==1
        assert db.get(SourceSettings,source_id(CURRENT)).config==original
        assert db.get(Provider,source_id(CURRENT)).enabled
    down,up,live=view(clock+timedelta(seconds=1))
    assert live and down.live and up.live
    assert down.rows[0].current_seal_cny==18000
    assert up.rows[0].current_seal_cny==55000
    assert down.rows[0].node_amounts=={'09:15':None,'09:20':18000,'09:25':None}
    assert down.book_coverage==down.book_expected==2
    assert down.rows[0].pct_change==pytest.approx(-10)
    assert '买一' in up.seal_basis and '卖一' in down.seal_basis
    response=client.get('/api/v1/auction-support',params={'trade_day':str(DAY),'as_of_time':(clock+timedelta(seconds=1)).isoformat()})
    assert response.status_code==200,response.text
    assert response.json()['data']['down_days'][0]['rows'][0]['current_seal_cny']==18000
    assert response.json()['data']['up_trial_days'][0]['rows'][0]['current_seal_cny']==55000
    early=view(clock-timedelta(seconds=1))
    assert early[0].rows==[] and early[0].book_state=='waiting'


def test_incomplete_book_without_candidates_is_partial_instead_of_market_zero(client,monkeypatch):
    setup(client,monkeypatch)
    original=body
    def no_limit_books(clock,volume):
        rows=[]
        for line in original(clock,volume).decode('gb18030').splitlines():
            prefix,raw=line.split('="',1);fields=raw.removesuffix('";').split('~')
            fields[9]=fields[19]='10'
            rows.append(prefix+'="'+'~'.join(fields)+'";')
        return '\n'.join(rows).encode('gb18030')
    monkeypatch.setattr(__import__(__name__),'body',no_limit_books)
    monkeypatch.setattr('app.market.tdx_full_minute.universe',lambda db,config:[*KEYS,InstrumentKey('SSE','600003','stock')])
    clock=RealDateTime(2026,10,26,9,20,6,tzinfo=SHANGHAI)
    collect(monkeypatch,clock)
    down,up,_=view(clock+timedelta(seconds=1))
    assert down.rows==up.rows==[]
    assert down.book_state==up.book_state=='partial'
    assert down.book_coverage==2 and down.book_expected==3


def test_closed_view_retains_earlier_nodes_for_final_matched_cohort(client,monkeypatch):
    setup(client,monkeypatch)
    first=RealDateTime(2026,10,26,9,20,6,tzinfo=SHANGHAI)
    collect(monkeypatch,first)
    collect(monkeypatch,first.replace(minute=24),volume='0')
    with Session(engine()) as db:
        reader=SavedTables(db,first.replace(hour=15),DAY)
        from app.market.auction_book_read import latest_book_day
        assert latest_book_day(reader)==DAY
        down,_,live=book_views(reader,DAY,down_codes=[KEYS[0].symbol+'.SH'])
    assert not live
    assert down.rows[0].node_amounts['09:20']==18000
    assert down.rows[0].current_seal_cny is None


def test_current_seal_updates_but_stage_and_receipt_cutoff_stay_frozen(client,monkeypatch):
    setup(client,monkeypatch)
    first=RealDateTime(2026,10,26,9,20,6,tzinfo=SHANGHAI)
    collect(monkeypatch,first)
    collect(monkeypatch,first+timedelta(seconds=60),volume='40')
    down,*_=view(first+timedelta(seconds=61))
    assert down.rows[0].current_seal_cny==36000
    assert down.rows[0].node_amounts['09:20']==18000
    assert view(first+timedelta(seconds=1))[0].rows[0].current_seal_cny==18000


@pytest.mark.parametrize('change',['permission','lease','scope'])
def test_inflight_revocation_or_expired_lease_does_not_publish_book(client,monkeypatch,change):
    setup(client,monkeypatch)
    def action():
        with Session(engine()) as db,db.begin():
            if change=='permission':
                p=db.get(Provider,source_id(FAMILY));p.rights={**p.rights,'cache':'denied'}
            elif change=='scope':
                monkeypatch.setattr('app.market.tdx_full_minute.universe',lambda db,config:KEYS[:1])
            else:
                job=db.scalar(select(Job).where(Job.state=='running',Job.payload['source_id'].astext==str(source_id(FAMILY))))
                job.lease_until=RealDateTime(2000,1,1,tzinfo=SHANGHAI)
    collect(monkeypatch,RealDateTime(2026,10,26,9,20,6,tzinfo=SHANGHAI),action=action)
    assert evidence_count()==0


def test_post_auction_failure_keeps_saved_book_and_stale_books_do_not_look_live(client,monkeypatch):
    setup(client,monkeypatch)
    first=RealDateTime(2026,10,26,9,20,6,tzinfo=SHANGHAI)
    collect(monkeypatch,first)
    assert evidence_count()==1
    collect(monkeypatch,first.replace(minute=30))
    assert evidence_count()==1
    down,*_=view(first.replace(minute=24))
    assert not down.live and down.book_state=='stale' and down.rows==[]


def test_calendar_and_restart_alignment_do_not_poll_during_closed_session(client,monkeypatch):
    setup(client,monkeypatch)
    from app.market.realtime_schedule import FAMILIES,align_realtime,deferred_realtime
    from app.market.auction_schedule import next_collection
    from app.market.auction_schedule import schema_recheck
    assert FAMILY in FAMILIES
    assert schema_recheck(FAMILY,'schema_changed')
    assert not schema_recheck(FAMILY,'permission_missing')
    before=RealDateTime(2026,10,26,8,59,tzinfo=SHANGHAI)
    assert align_realtime(FAMILY,before)==before.replace(hour=9,minute=15)
    after=before.replace(hour=9,minute=26)
    assert next_collection(FAMILY,after,60)==before.replace(day=27,hour=9,minute=15)
    # No exchange calendar in this fixture: even a Monday cannot become an
    # authorized open session just from its weekday.
    with Session(engine()) as db:
        assert deferred_realtime(db,FAMILY,before.replace(hour=9,minute=20),60) is not None
