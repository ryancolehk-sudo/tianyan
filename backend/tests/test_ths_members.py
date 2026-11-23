"""Synthetic memberships; no network, isolated PostgreSQL only."""
from datetime import timedelta
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db import engine
from app.evidence.models import SourceVersion
from app.market.collect import store_reference_batch
from app.market.models import SourceSettings
from app.market.protocol import Response, SourceFailure
from app.market.service import source_id
from app.market.ths_members import MembershipPages, request_page
from app.market.tushare_market import encoded
from app.models import Provider
from test_terminal_workbench import setup, save, view, AT
from test_tushare_market import quote


def response(rows, stamp=AT):
    return Response(encoded(['ts_code','con_code','con_name'],rows),stamp,1,200)


def row(board='885800.TI', code='000001.SZ'):
    return {'ts_code':board,'con_code':code,'con_name':'合成股票'}


def test_pager_rejects_overlap_and_requires_completion(monkeypatch):
    monkeypatch.setattr('app.market.ths_members.PAGE_SIZE',2)
    pages=MembershipPages()
    pages.add(response([row(),row(code='000002.SZ')]))
    with pytest.raises(SourceFailure,match='pagination_limit_reached'):pages.batch()
    with pytest.raises(SourceFailure,match='pagination_overlap'):pages.add(response([row()]))
    pages.add(response([]))
    batch=pages.batch()
    assert len(batch.records)==2 and batch.records[-1][1].rows[0]['memberships']=='2'
    assert batch.records[0][1].rows[0]['capture_id']==batch.records[-1][1].rows[0]['capture_id']


def test_all_board_endpoint_preserves_hong_kong_rows_without_inventing_a_share_codes():
    pages=MembershipPages()
    pages.add(response([row('700012R.TI','0001.HK'),row('700012.TI','BRK.B.US'),row('861001.TI','ACDC.O'),row('861001.TI','AROC.N'),row()]))
    assert pages.batch().records[0][1].rows[0]['con_code']=='0001.HK'
    with pytest.raises(SourceFailure,match='instrument_mismatch'):
        MembershipPages().add(response([row(code='unknown')]))


def test_page_ceiling_never_publishes_a_truncated_capture(monkeypatch):
    monkeypatch.setattr('app.market.ths_members.PAGE_SIZE',1)
    monkeypatch.setattr('app.market.ths_members.MAX_PAGES',1)
    pages=MembershipPages()
    pages.add(response([row()]))
    with pytest.raises(SourceFailure,match='pagination_limit_reached'):pages.batch()
    with pytest.raises(SourceFailure,match='pagination_limit_reached'):pages.add(response([]))


def test_transport_retry_is_bounded_and_does_not_retry_permissions_or_schema():
    from unittest.mock import Mock
    good=response([row()])
    call=Mock(side_effect=[SourceFailure('transport_error'),good])
    assert request_page(call) is good and call.call_count==2
    for reason,expected in [('timeout',3),('permission_missing',1),('rate_limited',1),('schema_changed',1)]:
        call=Mock(side_effect=SourceFailure(reason))
        with pytest.raises(SourceFailure):request_page(call)
        assert call.call_count==expected


def test_ths_multiple_themes_cutoff_and_permission_are_independent_of_dc(client,monkeypatch):
    monkeypatch.setattr('app.market.ths_members.PAGE_SIZE',2)
    setup(client,monkeypatch,['tushare_market_rt','tushare_ths_index','tushare_ths_member'])
    with Session(engine()) as db,db.begin():
        for descriptor in list(db.scalars(select(SourceVersion))):
            db.add(SourceVersion(source_id=descriptor.source_id,descriptor=descriptor.descriptor,content_hash=descriptor.content_hash,recorded_at=AT-timedelta(days=2)))
        save(db,'tushare_market_rt',[quote('000001.SZ')])
        save(db,'tushare_ths_index',[
            {'ts_code':'885800.TI','name':'合成芯片','type':'N','exchange':'A'},
            {'ts_code':'885801.TI','name':'合成机器人','type':'N','exchange':'A'},
            {'ts_code':'881001.TI','name':'不是概念','type':'I','exchange':'A'}],AT-timedelta(minutes=10))
        pages=MembershipPages()
        pages.add(response([row(),row('885801.TI')]))
        pages.add(response([row('881001.TI')]))
        provider=db.get(Provider,source_id('tushare_ths_member'))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),pages.batch(),AT)
    stock=next(item for item in view(client)['stocks'] if item['code']=='000001.SZ')
    assert set(stock['ths_themes'])=={'合成芯片','合成机器人'}
    assert stock['ths_themes_received_at']
    prior=view(client,params='&as_of_time=2026-10-23T02:29:00Z')
    assert all(not item['ths_themes'] for item in prior['stocks'])
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('tushare_ths_member'))
        provider.rights={**provider.rights,'display':'denied'}
    assert all(not item['ths_themes'] for item in view(client)['stocks'])


def test_later_membership_does_not_backfill_previous_day(client,monkeypatch):
    setup(client,monkeypatch,['tushare_market_rt','tushare_ths_index','tushare_ths_member'])
    with Session(engine()) as db,db.begin():
        for descriptor in list(db.scalars(select(SourceVersion))):
            db.add(SourceVersion(source_id=descriptor.source_id,descriptor=descriptor.descriptor,content_hash=descriptor.content_hash,recorded_at=AT-timedelta(days=2)))
        save(db,'tushare_market_rt',[quote('000001.SZ')])
        save(db,'tushare_ths_index',[{'ts_code':'885800.TI','name':'合成芯片','type':'N','exchange':'A'}],AT-timedelta(minutes=1))
        pages=MembershipPages();pages.add(response([row()],AT+timedelta(days=1)))
        provider=db.get(Provider,source_id('tushare_ths_member'))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),pages.batch(),AT+timedelta(days=1))
    assert all(not item['ths_themes'] for item in view(client)['stocks'])


@pytest.mark.parametrize('status,age,allowed',[('error',120,True),('error',30,False),('complete',120,False)])
def test_failed_membership_probe_can_retry_but_success_keeps_six_hour_spacing(client,monkeypatch,status,age,allowed):
    from datetime import UTC,datetime
    from test_integration_data import configured,connection
    from app.auth import APIError
    from app.integrations.credentials import resolve_source_credentials,job_context
    from app.market.models import DataFetchRun
    from app.market.service import enqueue_collection
    configured(client,monkeypatch,('tushare_ths_member',))
    clock=datetime.now(UTC)
    with Session(engine()) as db,db.begin():
        connection(db,('tushare_ths_member',),enabled=False)
        identity=source_id('tushare_ths_member')
        db.add(DataFetchRun(source_id=identity,capability='reference.tushare',started_at=clock-timedelta(seconds=age+5),
            received_at=clock-timedelta(seconds=age),status=status,reason='instrument_mismatch' if status=='error' else None,
            request_count=1))
        db.get(SourceSettings,identity).next_run_at=clock+timedelta(hours=6)
        db.flush()
        bound=resolve_source_credentials(db,identity,'tushare_ths_member',allow_disabled=True)
        probe=job_context(bound,identity,probe=True)
        if allowed:assert enqueue_collection(db,identity,integration_probe=probe).state=='queued'
        else:
            with pytest.raises(APIError) as error:enqueue_collection(db,identity,integration_probe=probe)
            assert error.value.code=='source_rate_limit'
