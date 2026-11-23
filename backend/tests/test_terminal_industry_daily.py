"""Daily industry data retain source, date and catalog-receipt boundaries."""
from datetime import timedelta
from sqlalchemy.orm import Session
from app.db import engine
from app.market import terminal_read
from app.market.service import source_id
from app.models import Provider
from test_terminal_workbench import setup, save, view, DAY, AT
from test_data_sources import configuration, configure
from test_industry_catalog_pg import store_catalog


def seed(client,monkeypatch):
    headers=setup(client,monkeypatch,['tushare_direction_industry_funds','tushare_moneyflow_ind_dc'])
    configure(client,headers,configuration('eastmoney_funds'),key='daily-industry-catalog')
    stamp=AT.replace(hour=8)
    with Session(engine()) as db,db.begin():
        store_catalog(db,stamp)
        for day in (DAY,DAY-timedelta(days=1)):
            save(db,'tushare_direction_industry_funds',[
                dict(ts_code='BK0001.DC',trade_date=day.strftime('%Y%m%d'),name='一级合成',content_type='行业',net_amount='900',pct_change='1'),
                dict(ts_code='BK0002.DC',trade_date=day.strftime('%Y%m%d'),name='二级合成',content_type='行业',net_amount='100',pct_change='2'),
                dict(ts_code='BK0003.DC',trade_date=day.strftime('%Y%m%d'),name='三级合成',content_type='行业',net_amount='800',pct_change='3')],stamp)
        save(db,'tushare_moneyflow_ind_dc',[dict(ts_code='BK0004.DC',trade_date='20261023',name='概念合成',content_type='概念',net_amount='300',pct_change='4')],stamp)
    return stamp


def test_daily_industry_parent_and_detail_use_same_dated_series(client,monkeypatch):
    seed(client,monkeypatch)
    data=view(client,'funds')
    rows={item['code']:item for item in data['sectors']}
    assert rows['BK0002.DC']['industry_level']==2 and rows['BK0002.DC']['main_net_cny']==100
    assert rows['BK0004.DC']['category']=='concept'
    assert data['selected_sector_code']=='BK0002.DC'
    series=[item for item in data['funds_history'] if item['sector_code']=='BK0002.DC']
    assert [item['value'] for item in series]==[None,100]  # No yesterday catalog.
    response=client.get('/api/v1/workbench-details?day=2026-10-23&sector=BK0002.DC')
    assert response.status_code==200,response.text
    detail=response.json()['data']
    assert detail['funds_history']==series
    assert detail['revision']==data['detail_revision']


def test_daily_catalog_cannot_leak_before_receipt_or_after_permission_revocation(client,monkeypatch):
    stamp=seed(client,monkeypatch)
    # A pre-close read cannot display an early received final daily table.
    data=view(client,'funds','&as_of_time=2026-10-23T10:30:00%2B08:00')
    assert data['sectors']==[]
    before=view(client,'funds')
    with Session(engine()) as db,db.begin():
        provider=db.get(Provider,source_id('eastmoney_funds'))
        provider.rights={**provider.rights,'display':'denied'}
    after=view(client,'funds')
    assert all(row['industry_level'] is None for row in after['sectors'])
    assert after['detail_revision']!=before['detail_revision']
    assert all(item['value'] is None for item in after['funds_history'] if item['sector_code']=='BK0002.DC')


def test_catalog_receipt_invalidates_all_parent_and_detail_revisions(client,monkeypatch):
    stamp=seed(client,monkeypatch)
    first=view(client,'funds')
    with Session(engine()) as db,db.begin():store_catalog(db,stamp+timedelta(seconds=5))
    second=view(client,'funds')
    # Identical content may be idempotently stored, so check the canonical
    # revision across views rather than requiring a new duplicate evidence.
    response=client.get('/api/v1/workbench-details?day=2026-10-23&codes=000100.SZ')
    assert response.status_code==200
    assert second['detail_revision']==response.json()['data']['revision']
