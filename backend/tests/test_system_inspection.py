"""Exercise the real API, worker and evidence store in disposable PostgreSQL."""
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response
from app.models import Job, Provider
from app.worker import claim, process
from test_connected_data import source_config
from test_data_sources import configure, login

URL = '/api/v1/data-center/inspection'


def prepare(client, monkeypatch):
    headers = login(client)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    source = UUID(configure(client, headers, source_config('fed_public')).json()['data']['provider']['id'])
    earlier = datetime.now(UTC) - timedelta(days=2)
    with Session(engine()) as db, db.begin():
        db.get(SourceSettings, source).next_run_at = earlier
        db.add(DataFetchRun(source_id=source, capability='news.public', started_at=earlier, received_at=earlier,
            status='error', reason='upstream_unavailable', request_count=1, accepted=0))
    return headers, source


def post(client, headers, key='isolated-inspection-repair'):
    response = client.post(URL + '/repair', headers={**headers, 'Idempotency-Key':key}, json={})
    assert response.status_code == 200, response.text
    return response.json()['data']


def test_scan_then_one_click_collects_and_verifies_evidence(client, monkeypatch):
    headers, source = prepare(client, monkeypatch)
    scan = client.get(URL).json()['data']
    assert scan['checks']['数据类别'] == 31
    assert next(i for i in scan['findings'] if i['source_id'] == str(source) and i['area'] == 'source')['state'] == 'available'
    with Session(engine()) as db:
        assert not db.scalar(select(func.count(Job.id)))
    assert client.post(URL + '/repair', json={}).status_code == 403
    batch = post(client, headers)
    receipt = next(row for row in batch['actions'] if row['area'] == 'source')
    assert receipt['state'] == 'working' and receipt['job_id']
    assert post(client, headers) == batch
    assert post(client, headers, 'another-click-on-inspection')['actions'][0]['job_id'] == receipt['job_id']
    original = b'<rss><channel><item><title>inspected feed</title><link>https://example.com/item</link><pubDate>Thu, 02 Jan 2020 10:00:00 GMT</pubDate></item></channel></rss>'
    monkeypatch.setattr('app.market.collect.fetch_public', lambda *_:Response(original, datetime.now(UTC), 3, 200))
    job = claim()
    assert str(job.id) == receipt['job_id']
    process(job)
    assert len(client.get('/api/v1/market-news').json()['data']['items']) == 1
    after = client.get(URL).json()['data']
    assert after['last_repair']['actions'][0]['state'] == 'verified'
    assert not any(row['id'] == 'source:' + str(source) for row in after['findings'])
    with Session(engine()) as db, db.begin():
        db.get(Provider, source).enabled = False
    assert client.get(URL).json()['data']['last_repair']['actions'][0]['state'] == 'manual'


def test_waiting_source_is_tracked_until_its_own_later_collection(client, monkeypatch):
    from app.market.service import dispatch_scheduled
    headers, source = prepare(client, monkeypatch)
    future = datetime.now(UTC) + timedelta(hours=2)
    deadlines = {'fed_public': future}
    monkeypatch.setattr('app.market.budget.family_cooldown_until', lambda db,family,clock: deadlines.get(family))
    with Session(engine()) as db, db.begin():
        db.get(SourceSettings, source).next_run_at = future
    finding = next(f for f in client.get(URL).json()['data']['findings'] if f['id'] == 'source:' + str(source))
    assert finding['state'] == 'waiting' and finding['retry_at']
    item = next(r for r in post(client, headers)['actions'] if r['id'] == finding['id'])
    assert item['state'] == 'waiting' and item['job_id'] is None and item['capability'] == 'news.public'
    deadlines.clear()
    with Session(engine()) as db, db.begin():
        assert db.scalar(select(func.count(Job.id))) == 0
        config = db.get(SourceSettings, source)
        config.next_run_at = datetime.now(UTC)
        dispatch_scheduled(db, db.get(Provider, source), config, config.next_run_at)
    original = b'<rss><channel><item><title>scheduled recovery</title><link>https://example.com/news</link><pubDate>Thu, 02 Jan 2020 10:00:00 GMT</pubDate></item></channel></rss>'
    monkeypatch.setattr('app.market.collect.fetch_public', lambda *_:Response(original, datetime.now(UTC), 3, 200))
    process(claim())
    after = client.get(URL).json()['data']['last_repair']['actions']
    assert next(r for r in after if r['id'] == finding['id'])['state'] == 'verified'


def test_replaced_source_needs_no_manual_intervention(client, monkeypatch):
    headers, source = prepare(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        db.get(SourceSettings, source).paused_reason = 'replaced_by_fuyao'
    result = client.get(URL).json()['data']
    item = next(f for f in result['findings'] if f['id'] == 'source:' + str(source))
    assert item['state'] == 'limited' and '无需操作' in item['advice']
    assert all(row['source_id'] != str(source) for row in post(client, headers)['actions'])


def test_succeeded_job_without_complete_data_and_unrelated_receipts_never_pass(client, monkeypatch):
    headers, source = prepare(client, monkeypatch)
    action = post(client, headers)['actions'][0]
    with Session(engine()) as db, db.begin():
        job = db.get(Job, UUID(action['job_id']))
        job.state = 'succeeded'
        clock = datetime.now(UTC)
        for job_id,status,cap in [(None,'complete','news.public'),(job.id,'complete','unrelated.history'),(job.id,'partial','news.public')]:
            db.add(DataFetchRun(source_id=source,job_id=job_id,capability=cap,started_at=clock,received_at=clock,
                status=status,request_count=1, accepted=1,rejected=1 if status=='partial' else 0))
    assert client.get(URL).json()['data']['last_repair']['actions'][0]['state'] == 'manual'


def test_pause_preserved_and_reader_cannot_repair(client, monkeypatch):
    headers, source = prepare(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        db.get(SourceSettings, source).paused_reason = 'permission_missing'
    assert post(client, headers)['actions'] == []
    with Session(engine()) as db:
        assert not db.scalar(select(func.count(Job.id)))
        assert db.get(SourceSettings, source).paused_reason == 'permission_missing'


def test_reader_cannot_inspect_other_users_or_write(client):
    headers = login(client, role='viewer')
    assert client.get(URL).status_code == 403
    assert client.post(URL + '/repair', json={},headers={**headers,'Idempotency-Key':'readonly-inspection'}).status_code == 403


def test_historical_repairs_keep_each_date_and_unsupported_snapshot_separate(client, monkeypatch):
    from app.market.recovery import target
    from app.market.recovery_models import RecoveryCoverage
    from app.market.service import source_id
    from test_data_sources import configuration
    headers = login(client)
    monkeypatch.setattr(settings(), 'data_collection_enabled', True)
    monkeypatch.setattr(settings(), 'recovery_enabled', True)
    family = 'direction_em_hot'
    configure(client, headers, configuration(family))
    ident = source_id(family)
    stamp = datetime.now(UTC) - timedelta(days=2)
    with Session(engine()) as db, db.begin():
        provider, config = db.get(Provider, ident), db.get(SourceSettings, ident)
        config.next_run_at = datetime.now(UTC) + timedelta(hours=1)
        for offset in [1, 2]:
            row = target(db, provider, config, 'direction.daily_history', (stamp-timedelta(days=offset)).date(),
                         {'scope':'top10'}, 'day', stamp)
            row.state, row.reason = 'partial', 'historical_top_ten_incomplete'
        unsupported = target(db, provider, config, 'realtime.snapshots', stamp.date(), {'scope':'live'}, '1m', stamp)
        unsupported.state = 'unsupported'
    batch = post(client, headers)
    history = [item for item in batch['actions'] if item['area'] == 'history']
    assert len(history) == 2 and len({item['job_id'] for item in history}) == 2
    with Session(engine()) as db, db.begin():
        for item in history:
            job = db.get(Job, UUID(item['job_id']))
            assert job.kind == 'direction.archive' and job.payload['day'] == item['trade_day']
            job.state = 'succeeded'
        # A complete job for either date is insufficient without a complete dated coverage record.
        row = db.get(RecoveryCoverage, UUID(history[0]['coverage_id']))
        row.state, row.actual_count, row.checked_at = 'complete', 10, datetime.now(UTC)
    after = client.get(URL).json()['data']['last_repair']['actions']
    assert [item['trade_day'] for item in after if item['state']=='verified'] == [history[0]['trade_day']]
    assert post(client, headers, 'second-history-click')['actions'][0]['job_id'] == history[0]['job_id']


def test_coverage_finds_older_eligible_table_and_honors_corrections(client):
    from app.evidence.models import EvidenceVersion
    from app.market.service import source_id
    headers = login(client)
    configure(client, headers, source_config('worldbank_public'))
    ident = source_id('worldbank_public')
    stamp = datetime.now(UTC) - timedelta(hours=2)
    def evidence(key, version, dataset, rows, seconds):
        return EvidenceVersion(source_id=ident, visibility='shared',source_key=key,source_revision=str(version),
            version_number=version,kind='reference',received_at=stamp+timedelta(seconds=seconds),availability='unknown',
            provenance='source_reported',historical_eligible=False,rights_basis='fixture',content_hash='a'*64,
            payload={'dataset': dataset,'rows':rows})
    with Session(engine()) as db, db.begin():
        db.add(evidence('rates', 1, 'macro.rates', [{'indicator':'FR.INR.TEST','value':'4'}], 1))
        for i in range(101):
            db.add(evidence('other'+str(i), 1, 'macro.other', [{'value':'1'}], i+2))
    def state():
        return next(i for i in client.get('/api/v1/data-coverage').json()['data']['items'] if i['id']=='D26')['state']
    assert state() == 'partial'
    with Session(engine()) as db, db.begin():
        db.add(evidence('rates', 2, 'macro.rates', [], 200))
    assert state() == 'pending'
