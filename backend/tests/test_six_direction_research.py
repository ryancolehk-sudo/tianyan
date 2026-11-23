"""Synthetic records in a disposable database; never run against the real instance."""
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.db import engine
from app.market.review_comparison import compare_pools
from test_data_sources import login
from test_news_center import source, save


def table(day, rows, state='complete', source_id='same'):
    return dict(state=state, rows=[dict(trade_date=day, **row) for row in rows],
        scope='all', source_id=source_id, coverage_scope='date_scope_table', evidence_refs=[])


def test_comparison_missing_is_not_failure_and_duplicates_are_unknown():
    old_day, day=date(2026,10,30),date(2026,11,2)
    old=table(old_day,[dict(ts_code='600001.SH',limit='U',limit_times=2),dict(ts_code='600002.SH',limit='U',limit_times=1)])
    current=table(day,[dict(ts_code='600001.SH',limit='U',limit_times=3)])
    value=compare_pools(old,current,old_day,day)
    assert value['promoted_n']==1 and value['unknown_n']==1 and value['failed_n']==0
    assert value['promotion_pct'] is None
    current['rows'].append(current['rows'][0])
    assert compare_pools(old,current,old_day,day)['promoted_n']==0


def test_comparison_incomplete_or_different_origin_is_never_comparable():
    before,day=date(2026,10,30),date(2026,11,2)
    old=table(before,[dict(ts_code='600001.SH',limit='U',limit_times=2)])
    for current in [table(day,[],state='missing'),table(day,[dict(ts_code='600001.SH',limit='Z')],source_id='other')]:
        value=compare_pools(old,current,before,day)
        assert not value['comparable'] and value['failed_n']==0 and value['unknown_n']==1


def test_comparison_explicit_failure_and_nonconsecutive_board():
    before,day=date(2026,10,30),date(2026,11,2)
    old=table(before,[dict(ts_code='600001.SH',limit='U',limit_times=2),dict(ts_code='600002.SH',limit='U',limit_times=1)])
    current=table(day,[dict(ts_code='600001.SH',limit='Z'),dict(ts_code='600002.SH',limit='U',limit_times=4)])
    value=compare_pools(old,current,before,day)
    assert value['failed_n']==1 and value['unknown_n']==1 and value['promotion_pct'] is None


def test_topic_cutoff_uses_original_version_and_excludes_future_receipts(client):
    headers=login(client); origin=source(client,headers)
    now=datetime.now(UTC); original=now-timedelta(hours=2); cutoff=now-timedelta(hours=1)
    day=cutoff.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date()
    with Session(engine()) as db,db.begin():
        save(db,origin,'version','合成机器人原始材料',original,original)
        save(db,origin,'version','合成机器人更正材料',now,original)
        save(db,origin,'late','合成机器人迟到材料',now,original)
    params=dict(topic_source='dc',topic_key='BK0001.DC',keyword='机器人',trade_day=str(day),as_of_time=cutoff.isoformat())
    response=client.get('/api/v1/news/topics',params=params)
    assert response.status_code==200,response.text
    data=response.json()['data']
    assert [item['title'] for item in data['events']]==['合成机器人原始材料']
    assert data['events'][0]['basis']=='keyword_clue'
    assert data['members']==[] and data['gaps']


def test_topic_pagination_identity_permissions_and_latest_correction(client):
    headers=login(client); origin=source(client,headers)
    now=datetime.now(UTC)-timedelta(seconds=10)
    day=now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date()
    with Session(engine()) as db,db.begin():
        for i in range(3): save(db,origin,str(i),'合成机器人'+str(i),now,now)
        save(db,origin,'corrected','合成机器人旧文',now-timedelta(seconds=5),now-timedelta(seconds=5))
        save(db,origin,'corrected','不再匹配的更正',now,now)
    params=dict(topic_source='provider-a',topic_key='robot',keyword='机器人',trade_day=str(day),limit=1)
    first=client.get('/api/v1/news/topics',params=params)
    assert first.status_code==200,first.text
    data=first.json()['data']; assert data['total']==3 and data['next_cursor']
    second=client.get('/api/v1/news/topics',params={**params,'cursor':data['next_cursor']})
    assert second.status_code==200 and second.json()['data']['events'][0]['id']!=data['events'][0]['id']
    assert client.get('/api/v1/news/topics',params={**params,'topic_source':'provider-b','cursor':data['next_cursor']}).status_code==422
    from app.models import Provider
    with Session(engine()) as db,db.begin(): db.get(Provider,origin).enabled=False
    assert client.get('/api/v1/news/topics',params=params).json()['data']['total']==0


def test_topic_rejects_cutoff_on_another_day(client):
    login(client)
    response=client.get('/api/v1/news/topics',params=dict(topic_source='dc',topic_key='BK0001.DC',keyword='机器人',trade_day='2026-10-30',as_of_time='2026-11-02T10:00:00+08:00'))
    assert response.status_code==422
