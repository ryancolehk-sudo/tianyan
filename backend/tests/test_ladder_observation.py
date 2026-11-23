"""Ladder evidence boundaries over synthetic archived/live records."""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from io import BytesIO
from zipfile import ZipFile
from xml.etree import ElementTree
import pytest

from app.auth import APIError
from app.market import ladder_observation as ladder

DAY, PREVIOUS = date(2001,12,31), date(2001,12,28)
CUTOFF = datetime.fromisoformat('2001-12-31T10:00:00+08:00')


def pool(code='999991.SZ', **values):
    return {'ts_code':code,'name':'合成甲','limit':'U','limit_times':'2.0','pct_chg':'10',
            'close':'11','first_time':'92501','last_time':'14:50:01', **values}


def stock(**values):
    return SimpleNamespace(code='999991.SZ',name='合成甲',price=11,limit_up=11,high=11,
        pct_change=10,amount_cny=10000,seal_amount_cny=0,price_basis='intraday',
        source_time='2001-12-31T10:00:00+08:00',source_received_at=CUTOFF, **values)


def test_native_heights_times_and_zero_values_survive_without_mainboard_filter():
    row=pool(code='301697.SZ',limit_times='3.0',seal_amount_yuan='0',actual_turnover='0',change_reason='光伏+机器人+光伏')
    result=ladder.build_day(DAY,PREVIOUS,[row],[],[{'ts_code':'301697.SZ','close':'10','pct_chg':'0'}],complete=True)
    item=result.rows[0]
    assert (item.height,item.first_time,item.last_time)==(3,'09:25:01','14:50:01')
    assert item.pct_change==0 and item.seal_amount==0 and item.actual_turnover==0
    assert item.themes==['光伏','机器人'] and item.theme_source=='同花顺涨停原因'
    assert item.source_time is None and item.price_basis=='daily_close'


def test_elimination_requires_complete_pool_and_same_day_price_and_clears_yesterday_metrics():
    prior=pool(limit_times='2',seal_amount_yuan='999',actual_turnover='18')
    for complete,quote,expected in [(False,{'close':'10'},0),(True,{},0),(True,{'close':'10','pct_chg':'-1','amount':'5'},1)]:
        result=ladder.build_day(DAY,PREVIOUS,[],[prior],[{'ts_code':prior['ts_code'],**quote}],complete=complete)
        assert len(result.eliminated)==expected
        if expected:
            row=result.eliminated[0]
            assert row.height==3 and row.status=='eliminated' and row.amount==5000
            assert row.first_time is row.last_time is row.seal_amount is row.actual_turnover is None


def test_native_broken_pool_is_distinct_from_elimination_and_duplicates_are_excluded():
    broken=pool(limit='Z',first_time='09:30:00',last_time='10:30:00')
    result=ladder.build_day(DAY,PREVIOUS,[broken],[pool()], [{'ts_code':'999991.SZ','close':'10'}],complete=True)
    assert result.eliminated[0].status=='opened'
    assert result.eliminated[0].first_time=='09:30:00'
    assert not ladder.build_day(DAY,PREVIOUS,[pool(),pool()],[],[],complete=True).rows
    assert not ladder.build_day(DAY,PREVIOUS,[pool(),pool()],[pool()],[{'ts_code':'999991.SZ','close':'10'}],complete=True).eliminated


def test_live_book_required_for_sealed_quote_only_is_touched_and_late_quotes_excluded():
    target=ladder.LadderDay(day=DAY,previous_day=PREVIOUS,state='missing')
    quote=stock()
    result=ladder.apply_live(target,{'999991.SZ':quote},[pool()],[],[],CUTOFF)
    assert target.rows[0].status=='touched' and target.rows[0].height==3
    assert target.rows[0].first_time is None
    quote.seal_amount_cny=100
    ladder.apply_live(target,{'999991.SZ':quote},[pool()],[],[],CUTOFF)
    assert target.rows[0].status=='sealed'
    for bad in ('2001-12-31T10:01:00+08:00','2001-12-28T10:00:00+08:00','2001-12-31T09:00:00+08:00'):
        quote.source_time=bad
        assert not ladder.apply_live(target,{'999991.SZ':quote},[pool()],[],[],CUTOFF)
        assert not target.rows and not target.eliminated


def test_eliminated_down_pool_does_not_become_an_up_seal_or_first_seal():
    down=pool(limit='D',first_time='09:30:00',last_time='14:00:00',seal_amount_yuan='12345')
    result=ladder.build_day(DAY,PREVIOUS,[down],[pool(change_reason='原日题材')],
        [{'ts_code':'999991.SZ','close':'9','pct_chg':'-10'}],complete=True)
    row=result.eliminated[0]
    assert row.status=='eliminated' and row.first_time is row.last_time is row.seal_amount is None
    assert row.themes==['原日题材'] and str(PREVIOUS) in row.theme_source


def test_unknown_previous_height_is_not_guessed_from_pool_absence_and_stale_does_not_promote():
    target=ladder.LadderDay(day=DAY,state='missing')
    quote=stock()
    latest=ladder.apply_live(target,{'999991.SZ':quote},[],[],[],CUTOFF)
    assert latest and not target.rows
    previous=[{'ts_code':'999991.SZ','close':'10'}]
    limits=[{'ts_code':'999991.SZ','up_limit':'11'}]
    ladder.apply_live(target,{'999991.SZ':quote},[],previous,limits,CUTOFF)
    assert target.rows[0].height==1
    quote.source_time='2001-12-31T09:55:00+08:00'
    latest=ladder.apply_live(target,{'999991.SZ':quote},[pool()],[],[],CUTOFF)
    assert latest['999991.SZ'].stale and not target.rows


def test_intraday_archived_pool_is_not_read_and_cutoff_reaches_all_archive_reads():
    reader=Mock()
    pool_table,rows,basics=ladder.archived_pool(reader,DAY,False)
    assert pool_table['state']=='missing' and not rows
    meta=ladder._metadata(pool_table)
    assert meta['dataset']=='pools' and meta['trade_day']==str(DAY) and meta['scope']=='all'
    assert meta['requested_day']==str(DAY) and meta['row_count'] is None and meta['source_id'] is None
    assert not meta['evidence_refs'] and meta['received_at'] is None
    reader.get.assert_not_called()
    with patch.object(ladder,'now',return_value=CUTOFF):
        with pytest.raises(APIError):ladder.observation_view(object(),None,as_of_time=datetime.fromisoformat('2001-12-31T10:01:00+08:00'))


def test_calendar_missing_returns_empty_and_no_archive_or_live_fetch():
    with patch.object(ladder,'now',return_value=CUTOFF),patch.object(ladder,'_context',return_value=({},[],None)),patch.object(ladder,'ArchiveReader') as read,patch.object(ladder,'live_quotes') as live:
        view=ladder.observation_view(object(),None)
    assert view.state=='missing' and not view.days and not view.can_export
    read.assert_not_called();live.assert_not_called()


def test_selected_projection_filters_source_hash_and_codes_and_respects_revoked_manifest():
    reader=Mock();reader.get.return_value={'state':'permission_denied','rows':[]}
    assert not ladder.selected_rows(reader,'daily',DAY,{'999991.SZ'})['rows']
    reader.db.execute.assert_not_called()
    reader.get.return_value={'state':'complete','evidence_refs':[{'id':'00000000-0000-4000-8000-000000000001','content_hash':'a'*64,'source_id':'test'}]}
    reader.db.execute.return_value=[({'ts_code':'999991.SZ','trade_date':'20011231'},7)]
    result=ladder.selected_rows(reader,'daily',DAY,{'999991.SZ'})
    assert result['rows'][0]['row_reference']['source_row']==6
    sql=str(reader.db.execute.call_args.args[0])
    assert 'content_hash =' in sql and ' IN ' in sql and 'jsonb_array_elements' in sql


def test_xlsx_uses_real_workbook_and_formula_like_names_remain_strings():
    row=ladder.record(pool(name='=1+1'),DAY)
    view=ladder.LadderObservation(trade_day=DAY,state='complete',built_at=CUTOFF.isoformat(),days=[ladder.LadderDay(day=DAY,state='complete',rows=[row])])
    with pytest.raises(APIError):ladder.export_xlsx(view)
    view.can_export=True
    data=ladder.export_xlsx(view)
    with ZipFile(BytesIO(data)) as z:
        doc=ElementTree.fromstring(z.read('xl/worksheets/sheet1.xml'))
        ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        assert not doc.findall('.//s:f',ns)
        assert '=1+1' in z.read('xl/worksheets/sheet1.xml').decode()
        assert 'xl/workbook.xml' in z.namelist()


def test_endpoint_authentication_bounds_and_export_denial(client,monkeypatch):
    assert client.get('/api/v1/ladder-observation').status_code==401
    assert client.get('/api/v1/ladder-observation.xlsx').status_code==401
    from test_data_sources import login
    login(client)
    assert client.get('/api/v1/ladder-observation?lookback=21').status_code==422
    assert client.get('/api/v1/ladder-observation?as_of_time=2001-12-31T10:00:00').status_code==422
    response=client.get('/api/v1/ladder-observation?trade_day=2001-12-31')
    assert response.status_code==200 and response.json()['data']['days']==[]
    assert client.get('/api/v1/ladder-observation.xlsx?trade_day=2001-12-31').status_code==403


def test_original_auction_price_requires_yuan_and_source_permission(client,monkeypatch):
    from test_terminal_workbench import setup,save
    from app.db import engine
    from sqlalchemy.orm import Session
    from app.market.service import source_id
    from app.models import Provider
    from datetime import timedelta,UTC
    stamp=datetime.now(UTC)
    setup(client,monkeypatch,['tushare_stk_auction'])
    with Session(engine()) as db,db.begin():
        save(db,'tushare_stk_auction',[{'ts_code':'600100.SH','trade_date':'20011231','price':'11','pre_close':'10','amount':'5000','vol':'1000','volume_ratio':'2'}],stamp)
    with Session(engine()) as db:
        prices,provider=ladder.auction_prices(db,DAY,stamp+timedelta(seconds=1))
        assert prices=={'600100.SH':11} and provider is not None
        assert ladder.auction_prices(db,DAY,stamp-timedelta(seconds=1))[0]=={}
    with Session(engine()) as db,db.begin():
        db.get(Provider,source_id('tushare_stk_auction')).enabled=False
    with Session(engine()) as db:
        assert ladder.auction_prices(db,DAY,stamp+timedelta(seconds=1))[0]=={}
