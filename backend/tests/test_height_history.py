"""Synthetic history/calendar/permission tests on a disposable database."""
from datetime import UTC, date, datetime, timedelta
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.db import engine
from app.models import Job
from app.market import height_history as module
from test_data_sources import login

DAY = date(2026, 11, 6)


def pool(code='600001.SH', height='5', name='合成最高'):
    return dict(ts_code=code, trade_date='20261106', limit='U', limit_times=height, name=name)


def test_height_distinct_second_and_all_tied_leader_names():
    rows=[pool(),pool('600002.SH','5','合成并列'),pool('300001.SZ','3','合成创业'),pool('600003.SH','2','合成二板')]
    point=module.height_point(DAY,rows)
    assert point['values']==dict(height=5,second_height=3,gem_height=3)
    assert point['leaders']['height']==['合成最高','合成并列']
    assert point['leaders']['gem_height']==['合成创业']


@pytest.mark.parametrize('value',[None,'bad','-1','0','3.5',True,'NaN'])
def test_unknown_or_invalid_board_height_does_not_fabricate_zero(value):
    assert module.height_point(DAY,[pool(height=value)])['values']['height'] is None


def test_duplicate_members_do_not_silently_change_a_curve():
    point=module.height_point(DAY,[pool(),pool()])
    assert point['state']=='unavailable' and not point['leaders']['height']


def test_one_day_below_highest_and_no_gem_are_known_absences():
    point=module.height_point(DAY,[pool()])
    assert point['values']==dict(height=5,second_height=0,gem_height=0)


def test_each_historical_day_keeps_its_native_names_cutoff_and_missing_gap(monkeypatch):
    days=[DAY-timedelta(days=2),DAY-timedelta(days=1),DAY]
    cutoff=datetime.fromisoformat('2026-11-06T17:00:00+08:00')
    monkeypatch.setattr(module,'available_trade_days',lambda *a,**k:days)
    calls=[]
    def load(db,dataset,day,as_of_time=None):
        calls.append((dataset,day,as_of_time))
        rows=[dict(pool(name='合成'+str(day)),trade_date=day.strftime('%Y%m%d'))] if day!=days[1] else []
        return dict(state='complete',rows=rows,row_count=len(rows),evidence_refs=[])
    monkeypatch.setattr(module,'load_dataset',load)
    result=module.height_history_view(object(),DAY,60,cutoff)
    assert len(result['records'])==3
    assert result['records'][1]['values']['height'] is None
    assert result['records'][0]['leaders']['height']==['合成'+str(days[0])]
    assert all(item[2]==cutoff for item in calls)


def test_unknown_date_is_preserved_not_replaced_with_latest(monkeypatch):
    monkeypatch.setattr(module,'available_trade_days',lambda *a,**k:[DAY])
    result=module.height_history_view(object(),DAY-timedelta(days=1),as_of_time=datetime(2026,11,9,tzinfo=UTC))
    assert result['records']==[] and result['trade_day']=='2026-11-05'


def test_height_endpoint_is_authenticated_and_get_has_no_jobs_or_vendor_io(client,monkeypatch):
    assert client.get('/api/v1/review-data/heights').status_code==401
    login(client,role='researcher')
    monkeypatch.setattr('urllib.request.build_opener',lambda *a:pytest.fail('read must not fetch'))
    with Session(engine()) as db: before=db.scalar(select(func.count()).select_from(Job))
    response=client.get('/api/v1/review-data/heights?trade_day=2026-11-06')
    assert response.status_code==200 and response.json()['data']['records']==[]
    assert client.get('/api/v1/review-data/heights?lookback=999').status_code==422
    assert client.get('/api/v1/review-data/heights?as_of_time=2099-01-01T00:00:00Z').status_code==422
    with Session(engine()) as db: assert db.scalar(select(func.count()).select_from(Job))==before
