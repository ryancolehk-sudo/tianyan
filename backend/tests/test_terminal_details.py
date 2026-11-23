"""Bounded read-only details, compared with the existing audited projection."""
from datetime import datetime, timedelta
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.db import engine
from app.models import Job, Provider, User
from app.market.models import DataFetchRun
from app.market.service import source_id
from app.market import terminal_read
from test_terminal_workbench import setup, save, view, AT, DAY
from test_tushare_market import quote


def test_history_reuses_limit_versions_without_backdating_corrections():
    from types import SimpleNamespace
    from collections import Counter

    calls=Counter()
    code='603278.SH'
    metas=[SimpleNamespace(id=key,family=terminal_read.LIMITS,dates={DAY},received_at=stamp)
        for key,stamp in [('corrected',AT+timedelta(minutes=3)),
                          ('missing',AT+timedelta(minutes=2)),('original',AT-timedelta(minutes=1))]]
    payloads={'original':[dict(ts_code=code,trade_date='20261023',up_limit='11',down_limit='9')],
        'corrected':[dict(ts_code=code,trade_date='20261023',up_limit='12',down_limit='8')],
        'missing':[dict(ts_code='000001.SZ',trade_date='20261023',up_limit='20',down_limit='10')]}
    def evidence(meta):
        calls[meta.id]+=1
        return SimpleNamespace(payload={'rows':payloads[meta.id]})
    reader=SimpleNamespace(reference_meta=metas,evidence=evidence,at=terminal_read.at,
        cutoff=AT+timedelta(hours=1))
    snapshots=[SimpleNamespace(id=i,received_at=AT+timedelta(minutes=i)) for i in range(6)]
    rows=[(snapshot.id,{**quote(code,price='11',at=snapshot.received_at),
        'bid_price1':'11','bid_volume1':'100','ask_volume1':'0'}) for snapshot in snapshots]
    item=SimpleNamespace(first_observed=None,latest_change=None)
    events,seals=terminal_read.stock_history(reader,DAY,snapshots,code,item,rows)
    assert [point.value for point in seals]==[1100,1100,1100,None,None,None]
    assert item.first_observed=='10:30:00'
    assert len(events)==2
    # Work scales with immutable versions, not the number of observed minutes;
    # missing rows are reused too and may still fall back to earlier evidence.
    assert calls=={'original':1,'missing':1,'corrected':1}

def details(client, codes='000100.SZ', suffix=''):
    response=client.get('/api/v1/workbench-details?day=2026-10-23&codes='+codes+suffix)
    assert response.status_code==200,response.text
    return response.json()['data']

def test_batch_details_equal_selected_projection_without_market_work(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_market_rt',[quote('000100.SZ',price='10'),quote('000101.SZ',price='11')])
    original={code:view(client,'ladder','&stock='+code) for code in ['000100.SZ','000101.SZ']}
    monkeypatch.setattr(terminal_read,'breadth_projection',lambda *a,**k:pytest.fail('batch must not recompute breadth'))
    monkeypatch.setattr(terminal_read,'index_projection',lambda *a,**k:pytest.fail('batch must not reread indices'))
    data=details(client,'000100.SZ,000101.SZ')
    assert data['sector_code'] is None and data['members']==[] and data['funds_history']==[]
    for row in data['stocks']:
        old=original[row['code']]
        assert row==next(item for item in old['stocks'] if item['code']==row['code'])
        assert data['seals'][row['code']]==old['stock_seals']
        assert [event for event in data['events'] if event['code']==row['code']]==old['events']
    assert data['revision']==original['000100.SZ']['detail_revision']
    with Session(engine()) as db:
        assert db.scalar(select(func.count()).select_from(Job))==0
        assert db.scalar(select(func.count()).select_from(DataFetchRun))==0

def test_catalog_covers_beyond_visible_cap_and_detail_has_real_tail_quote(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    with Session(engine()) as db,db.begin():save(db,'tushare_market_rt',[quote(f'{i:06}.SZ',price='10') for i in range(100,350)])
    base=view(client)
    assert len(base['stocks'])==250 and len(base['detail_codes'])==250
    # Overview now includes the whole ranking; the ladder retains its visible
    # cap while still allowing details for any saved code beyond that cap.
    ladder=view(client,'ladder')
    assert len(ladder['stocks'])==200 and len(ladder['detail_codes'])==250
    result=details(client,'000349.SZ')
    assert result['stocks'][0]['price']==10 and result['revision']==base['detail_revision']

def test_revision_changes_on_new_evidence_and_revocation(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    with Session(engine()) as db,db.begin():save(db,'tushare_market_rt',[quote('000100.SZ',price='10')])
    first=details(client)
    with Session(engine()) as db,db.begin():save(db,'tushare_market_rt',[quote('000100.SZ',price='11',at=AT+timedelta(minutes=1))],AT+timedelta(minutes=1))
    latest=details(client)
    assert latest['revision']!=first['revision'] and latest['stocks'][0]['price']==11
    observed=details(client,suffix='&as_of_time=2026-10-23T10:30:30%2B08:00')
    assert observed['stocks'][0]['price']==10
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_market_rt'));provider.rights={**provider.rights,'display':'denied'}
    denied=details(client)
    assert denied['revision']!=latest['revision'] and denied['stocks'][0]['price'] is None
    assert denied['seals']['000100.SZ']==[]


def test_parent_observation_context_does_not_chase_later_receipts(client, monkeypatch):
    from urllib.parse import urlencode

    setup(client, monkeypatch, ['tushare_market_rt'])
    captured = AT + timedelta(seconds=30, microseconds=123456)
    monkeypatch.setattr(terminal_read, 'now', lambda db: captured)
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='10')])
    parent = view(client)
    later = AT + timedelta(minutes=1)
    monkeypatch.setattr(terminal_read, 'now', lambda db: later + timedelta(seconds=30))
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='11', at=later)], later)
    pinned = details(client, suffix='&' + urlencode({'observation_cutoff': captured.isoformat()}))
    # New receipts must not make a detail requested by the existing parent
    # chase a moving target, or replace its values with a different snapshot.
    assert pinned['revision'] == parent['detail_revision']
    assert pinned['stocks'][0]['price'] == 10
    assert datetime.fromisoformat(parent['detail_observation_cutoff']) == captured
    latest = view(client)
    current = details(client, suffix='&' + urlencode({'observation_cutoff': latest['detail_observation_cutoff']}))
    assert current['stocks'][0]['price'] == 11
    assert current['revision'] == latest['detail_revision'] != parent['detail_revision']

@pytest.mark.parametrize('query',['codes=bad','codes='+','.join(f'{i:06}.SZ' for i in range(25)), 'sector=bad','day=2099-01-01','as_of_time=2099-01-01T00:00:00Z',
    'observation_cutoff=2099-01-01T00:00:00Z', 'observation_cutoff=2026-10-23T10:30:00',
    'observation_cutoff=2026-10-23T02:30:00Z&as_of_time=2026-10-23T02:30:00Z'])
def test_detail_validation(client,monkeypatch,query):
    setup(client,monkeypatch,[])
    prefix='' if query.startswith('day=') else 'day=2026-10-23&'
    assert client.get('/api/v1/workbench-details?'+prefix+query).status_code==422


def test_pinned_details_keep_parent_phase_across_close(client, monkeypatch):
    from urllib.parse import urlencode

    setup(client, monkeypatch, ['tushare_market_rt'])
    captured = AT.replace(hour=6, minute=59)  # 14:59 Shanghai
    monkeypatch.setattr(terminal_read, 'now', lambda db: captured)
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='10', at=captured)], captured)
    parent = view(client)
    monkeypatch.setattr(terminal_read, 'now', lambda db: captured + timedelta(minutes=2))
    pinned = details(client, suffix='&' + urlencode({'observation_cutoff': parent['detail_observation_cutoff']}))
    assert pinned['revision'] == parent['detail_revision'] and pinned['stocks'][0]['price'] == 10
    assert details(client)['revision'] != parent['detail_revision']

def test_details_require_session(client):
    assert client.get('/api/v1/workbench-details?day=2026-10-23&codes=000100.SZ').status_code==401


def test_calendar_change_invalidates_details_even_without_new_quotes(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt'])
    with Session(engine()) as db,db.begin():save(db,'tushare_market_rt',[quote('000100.SZ',price='10')])
    before=details(client)
    calendar=terminal_read.SavedTables.calendar
    monkeypatch.setattr(terminal_read.SavedTables,'calendar',lambda self:calendar(self)+[DAY-timedelta(days=7)])
    assert details(client)['revision']!=before['revision']


@pytest.mark.parametrize('as_of', [None, AT + timedelta(seconds=30)])
def test_four_parent_views_and_stock_details_share_one_canonical_manifest(client, monkeypatch, as_of):
    from test_data_sources import configuration, configure
    from test_intraday_funds import save as save_funds
    from app.evidence.models import EvidenceVersion

    headers = setup(client, monkeypatch, ['tushare_market_rt'])
    config = configuration('eastmoney_funds')
    config['interval_seconds'] = 150
    configure(client, headers, config, key='canonical-funds-fixture')
    with Session(engine()) as db, db.begin():
        save(db, 'tushare_market_rt', [quote('000100.SZ', price='10')])
        save_funds(db, [('BK0001', '20')])
        saved_funds_id = db.scalar(select(EvidenceVersion.id).where(
            EvidenceVersion.source_id == source_id('eastmoney_funds')))
    # All projections observe exactly the same committed database snapshot.
    # Funds' own input receipt belongs to its parent, not stock-detail identity.
    with Session(engine()) as db:
        db.connection(execution_options={'isolation_level': 'REPEATABLE READ', 'postgresql_readonly': True})
        owner = db.scalar(select(User.id))
        parents, manifests = [], []
        for kind in ('overview', 'ladder', 'funds', 'auction'):
            manifest = []
            parents.append(terminal_read.workbench_view(db, owner, view=kind, day=DAY,
                stock='000100.SZ', as_of_time=as_of, input_manifest=manifest))
            manifests.append(manifest)
        child = terminal_read.workbench_view(db, owner, day=DAY, as_of_time=as_of,
            detail_codes=['000100.SZ'])
        assert parents[0].intraday_funds.sectors and parents[2].intraday_funds.sectors
        assert all(any(item['evidence_id'] == str(saved_funds_id) for item in manifests[index])
                   for index in (0, 2))
        assert {parent.detail_revision for parent in parents} == {child.revision}
        assert child.stocks[0].price == 10
        with monkeypatch.context() as isolated:
            isolated.setattr(terminal_read, 'intraday_funds', lambda *a, **k: pytest.fail(
                'board details do not return minute funds or need their projection'))
            board = terminal_read.workbench_view(db, owner, day=DAY, as_of_time=as_of,
                sector='BK0001.DC', detail_codes=[])
            assert board.sector_code == 'BK0001.DC' and board.revision == child.revision
        before = child.revision
    with Session(engine()) as db, db.begin():
        save_funds(db, [('BK0001', '40')], AT + timedelta(seconds=10))
    with Session(engine()) as db:
        owner = db.scalar(select(User.id))
        parent = terminal_read.workbench_view(db, owner, view='funds', day=DAY, as_of_time=as_of)
        child = terminal_read.workbench_view(db, owner, day=DAY, as_of_time=as_of,
            detail_codes=['000100.SZ'])
        assert parent.intraday_funds.sectors[0].main_net_cny == 40
        assert parent.detail_revision == child.revision == before
