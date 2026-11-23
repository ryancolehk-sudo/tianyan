"""Dated supplier fallback preserves units, rights and immutable row lineage."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.market import review_fallback, review_read
from app.market.collect import store_reference_batch
from app.market.models import SourceSettings
from app.market.service import source_id
from app.models import Provider
from test_data_sources import configuration, configure, login
from test_mairui_reference import envelope, parse

CLOCK = datetime.fromisoformat('2026-10-26T17:00:00+08:00')
DAY = CLOCK.date()


def saved(symbol, *, money='100000000'):
    return SimpleNamespace(id=uuid.uuid4(), source_id=uuid.uuid4(), content_hash='a'*64,
        received_at=CLOCK-timedelta(minutes=10), payload={
            'columns':[{'key':'zj','unit':'元'},{'key':'cje','unit':'元'}],
            'rows':[dict(symbol=symbol, name='合成日榜', p='11', zf='10', lbc='2',
                zj=money,cje='200000000',first_limit_raw='093100',last_limit_raw='14:55:00',zbc='0')]})


def mocked(monkeypatch, values):
    provider=SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(review_fallback,'permitted_provider',lambda *a:provider)
    db=Mock();db.scalar.side_effect=values
    return db


def test_pool_rows_keep_original_evidence_offsets_and_verified_units(monkeypatch):
    sources=[saved('sh600001'),saved('000001'),saved('920001')]
    result=review_fallback.pool_table(mocked(monkeypatch,sources),DAY,CLOCK)
    assert result['row_count']==3 and result['basis']=='supplier_daily_pool'
    assert result['seal_amount_unit_verified'] is True and result['source_time'] is None
    assert [r['limit'] for r in result['rows']]==['U','D','Z']
    assert [r['ts_code'] for r in result['rows']]==['600001.SH','000001.SZ','920001.BJ']
    assert result['rows'][0]['fd_amount']=='100000000'
    assert result['rows'][0]['first_time']=='093100'
    assert review_read._row_reference(result,1)['id']==str(sources[1].id)
    assert review_read._row_reference(result,1)['source_row']==0
    assert '当前盘口' in result['coverage_scope']


def test_unknown_seal_unit_and_missing_values_never_become_zero(monkeypatch):
    sources=[saved('600001'),saved('000001',money=None),saved('920001')]
    sources[0].payload['columns'][0]['unit']='unknown'
    table=review_fallback.pool_table(mocked(monkeypatch,sources),DAY,CLOCK)
    assert table['seal_amount_unit_verified'] is False
    assert table['rows'][0]['fd_amount'] is None and table['rows'][1]['fd_amount'] is None


@pytest.mark.parametrize('bad', ['index','sh000001','sz600001','600001.SH'])
def test_bad_or_conflicting_security_id_is_not_relabelled(monkeypatch,bad):
    assert review_fallback.pool_table(mocked(monkeypatch,[saved(bad)]),DAY,CLOCK) is None


def test_missing_group_is_not_claimed_as_complete_pool(monkeypatch):
    assert review_fallback.pool_table(mocked(monkeypatch,[saved('600001'),None]),DAY,CLOCK) is None


def test_conflicting_pool_types_do_not_silently_win(monkeypatch):
    assert review_fallback.pool_table(mocked(monkeypatch,[saved('600001'),saved('600001')]),DAY,CLOCK) is None


def test_revoked_rights_and_future_day_do_not_read_rows(monkeypatch):
    monkeypatch.setattr(review_fallback,'permitted_provider',lambda *a:None)
    db=Mock()
    assert review_fallback.pool_table(db,DAY,CLOCK) is None
    assert review_fallback.pool_table(db,DAY+timedelta(days=1),CLOCK) is None
    db.scalar.assert_not_called()


@pytest.mark.parametrize('state', ['permission_denied','broken_lineage'])
def test_denied_or_broken_primary_cannot_be_hidden_by_fallback(monkeypatch,state):
    monkeypatch.setattr(review_read,'load_dataset',lambda *a,**k:{'state':state,'row_count':0})
    substitute=Mock()
    monkeypatch.setattr(review_fallback,'pool_table',substitute)
    assert review_read._Reader(object(),CLOCK).get('pools',DAY)['state']==state
    substitute.assert_not_called()


def test_empty_primary_uses_saved_pool_but_nonempty_primary_wins(monkeypatch):
    primary={'state':'complete','row_count':0,'rows':[]}
    monkeypatch.setattr(review_read,'load_dataset',lambda *a,**k:primary)
    substitute=Mock(return_value={'state':'complete','row_count':1,'rows':[{'name':'saved'}]})
    monkeypatch.setattr(review_fallback,'pool_table',substitute)
    assert review_read._Reader(object(),CLOCK).get('pools',DAY)['rows'][0]['name']=='saved'
    primary.update(row_count=1,rows=[{'name':'primary'}]);substitute.reset_mock()
    assert review_read._Reader(object(),CLOCK).get('pools',DAY)['rows'][0]['name']=='primary'
    substitute.assert_not_called()


def test_actual_storage_date_and_receipt_fences(client,monkeypatch):
    headers=login(client);configure(client,headers,configuration('mairui_pools'))
    received=datetime.now(CLOCK.tzinfo)
    value=envelope('mairui_pools')
    # Distinct identities are retained across the supplier's three lists.
    for group,code in zip(value['tables'],['sh600001','sz000001','bj920001'],strict=True):
        group['rows'][0]['dm']=code
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('mairui_pools'))
        store_reference_batch(db,provider,db.get(SourceSettings,provider.id),parse(value),received)
        old=datetime.fromisoformat(value['query_day']).date()
        assert review_fallback.pool_table(db,old)['row_count']==3
        assert review_fallback.pool_table(db,old,received-timedelta(seconds=1)) is None
        assert review_fallback.pool_table(db,old+timedelta(days=1)) is None
