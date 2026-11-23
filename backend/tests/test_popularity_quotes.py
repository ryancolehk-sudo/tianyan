from datetime import datetime, date, UTC
from types import SimpleNamespace as N
from decimal import Decimal
import pytest
from app.market.direction_read import enrich_popularity_quotes

@pytest.mark.parametrize('price_day,pct,expected', [(date(2026,11,3),Decimal('0'),'0'), (date(2026,11,3),Decimal('-2.51'),'-2.51'), (date(2026,11,2),Decimal('9'),None), (date(2026,11,3),None,None)])
def test_quotes_keep_day_zero_missing_and_provenance(monkeypatch,price_day,pct,expected):
    clock=datetime(2026,11,3,8,tzinfo=UTC)
    q=N(instrument=N(symbol='600001'),quote=N(trade_day=price_day,quote_time=None),change_percent=pct,state='daily_close',provenance=N(source_name='Authorized quote',received_at=clock))
    calls=[]
    def batch(db, **kwargs):
        calls.append(kwargs)
        return N(items=[q])
    monkeypatch.setattr('app.market.read_models.quote_batch',batch)
    db=N(scalars=lambda stmt: ['instrument-id'])
    panel=dict(rows=[dict(code='600001',rank=1)],source_label='Rank source',source_time=None,received_at=clock.isoformat())
    enrich_popularity_quotes(db,{'ths':panel},date(2026,11,3),None,clock)
    assert panel['rows'][0].get('pct_change') == expected
    assert panel['rows'][0]['rank'] == 1
    assert calls[0]['observation_cutoff'] == clock
    if expected is not None:
        assert panel['rows'][0]['quote_source'] == 'Authorized quote'
        assert panel['rows'][0]['quote_time'] is None


def test_historical_quote_cutoff_is_pinned(monkeypatch):
    clock=datetime(2026,11,3,8,tzinfo=UTC)
    calls=[]
    monkeypatch.setattr('app.market.read_models.quote_batch',lambda db,**kw: calls.append(kw) or N(items=[]))
    panel=dict(rows=[dict(code='600001',rank=1,pct_change='1.23')],source_label='Rank source',source_time=None,received_at=clock.isoformat())
    enrich_popularity_quotes(N(scalars=lambda stmt:['id']),{'ths':panel},date(2026,11,2),None,clock)
    assert calls[0]['as_of_time'].date() == date(2026,11,2)
    assert calls[0]['observation_cutoff'] is None
    assert panel['rows'][0]['pct_change'] == '1.23'
    assert panel['rows'][0]['quote_state'] == 'ranking_snapshot'
