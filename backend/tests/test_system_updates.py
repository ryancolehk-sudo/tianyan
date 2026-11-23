import base64
import hashlib
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_data_sources import login

from app.config import settings
from app.db import engine
from app.evidence.common import canonical
from app.maintenance_control import KEY, admission_engine, request_admission
from app.system_settings.models import MaintenanceJob
from app.system_settings.service import get_system_state, get_update_state
from app.updates import bridge, service
from app.updates.manifest import SERVICES, UpdateFailure, verified
from app.updates.transport import destination
from app.version import SCHEMA_REVISION, APP_VERSION

_major,_minor,_patch=map(int,APP_VERSION.split('.'))
NEXT_VERSION=f'{_major}.{_minor+1}.0'

sys.path.insert(0, '/workspace/scripts')
from update_host import Updater, UpgradeError, atomic_json  # noqa: E402


@pytest.fixture
def signed(monkeypatch, tmp_path):
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes_raw()).decode()
    monkeypatch.setattr(settings(), 'update_feed_url', 'https://updates.example.com/stable.json')
    monkeypatch.setattr(settings(), 'update_public_key', public)
    monkeypatch.setattr(settings(), 'update_directory', str(tmp_path))
    with Session(engine()) as db, db.begin():
        get_system_state(db).backup_heartbeat_at = datetime.now(UTC)
    document = {'format': 1, 'id': 'test-'+NEXT_VERSION, 'version': NEXT_VERSION, 'build_revision': 'a' * 40,
        'channel': 'stable', 'published_at': datetime.now(UTC).isoformat(), 'notes': 'Isolated fixture; not a public release',
        'automatic_supported': True, 'package_url': 'https://updates.example.com/program.tar',
        'package_sha256': hashlib.sha256(b'fixture package').hexdigest(), 'size_bytes': len(b'fixture package'),
        'unpacked_bytes': 1024, 'minimum_free_bytes': 1024**3, 'postgres_major': 17,
        'schema': {'from_revisions': [SCHEMA_REVISION], 'to_revision': SCHEMA_REVISION,
                   'compatible_revisions': [SCHEMA_REVISION], 'backward_compatible': True},
        'images': {name: 'sha256:' + 'b' * 64 for name in SERVICES}}
    def sign(value):
        return base64.b64encode(private.sign(canonical(value))).decode()
    payload = {'manifest': document, 'signature': sign(document)}
    calls = []
    def fetch(url, **values):
        calls.append(url)
        if values.get('output') is not None:
            values['output'].write(b'fixture package')
            return len(b'fixture package')
        return json.dumps(payload).encode()
    monkeypatch.setattr(service, 'fetch', fetch)
    return SimpleNamespace(document=document, signature=payload['signature'], public=public, calls=calls, sign=sign, payload=payload)


def key(headers, value):
    return {**headers, 'Idempotency-Key': value}


def test_unconfigured_and_admin_csrf(client):
    headers = login(client)
    value = client.get('/api/v1/system/updates').json()['data']
    assert value['state'] == 'unconfigured' and value['release'] is None
    assert '未配置' in value['message']
    assert client.post('/api/v1/system/updates/check', headers=key(headers, 'no-source')).json()['data']['state'] == 'unconfigured'
    assert client.post('/api/v1/system/updates/download', json={'release_id': 'anything'}).status_code == 403


def test_signature_tamper_and_transport_destinations(signed):
    assert verified(signed.document, signed.signature, signed.public).version == NEXT_VERSION
    with pytest.raises(UpdateFailure):
        verified({**signed.document, 'notes': 'tampered'}, signed.signature, signed.public)
    for address in ('http://updates.example.com/x', 'https://evil.example/x', 'https://user:pw@updates.example.com/x',
                    'https://updates.example.com/x?token=secret', 'https://updates.example.com:444/x'):
        with pytest.raises(UpdateFailure):
            destination(address, 'https://updates.example.com/stable.json', [])


def test_check_throttle_signed_download_idempotency_and_source_install(client, signed):
    headers = login(client)
    checked = client.post('/api/v1/system/updates/check', headers=key(headers, 'check-fixture')).json()['data']
    assert checked['release']['id'] == signed.document['id']
    assert checked['state'] == 'manual' and '源码' in checked['release']['reason']
    with Session(engine()) as db:
        service.maybe_check(db)
        service.maybe_check(db)
    assert len(signed.calls) == 1
    request = {'release_id': signed.document['id']}
    queued = client.post('/api/v1/system/updates/download', json=request, headers=key(headers, 'download-fixture'))
    assert queued.status_code == 202, queued.text
    replay = client.post('/api/v1/system/updates/download', json=request, headers=key(headers, 'download-fixture'))
    assert replay.status_code == 202 and replay.json() == queued.json()
    with Session(engine()) as db:
        job = db.scalar(select(MaintenanceJob).where(MaintenanceJob.kind == 'update.download'))
        result = service.run_download(db, job)
        assert result['size_bytes'] == signed.document['size_bytes']
        state = get_update_state(db)
        path = service.package_path(state.package_name)
        assert path.read_bytes() == b'fixture package'
        path.write_bytes(b'corrupted')
        service.run_download(db, job)
        assert path.read_bytes() == b'fixture package'
    response = client.post('/api/v1/system/updates/install', json={**request, 'confirmed': True, 'drafts_saved': True}, headers=key(headers, 'install-source'))
    assert response.status_code == 409 and response.json()['error']['code'] == 'manual_upgrade_required'


def test_bad_signature_never_replaces_release(client, signed):
    login(client)
    signed.payload['signature'] = 'invalid'
    with Session(engine()) as db:
        result = service.check(db)
        assert result.state == 'error' and result.release is None
        assert get_update_state(db).manifest is None


def test_download_failure_retains_existing_data_and_no_partial(client, signed, monkeypatch, tmp_path):
    headers = login(client)
    client.post('/api/v1/system/updates/check', headers=key(headers, 'check-damaged'))
    client.post('/api/v1/system/updates/download', json={'release_id': signed.document['id']}, headers=key(headers, 'download-damaged'))
    def broken(url, **values):
        values['output'].write(b'broken')
        raise UpdateFailure('update_connection_failed', 'download fixture failure')
    monkeypatch.setattr(service, 'fetch', broken)
    with Session(engine()) as db:
        job = db.scalar(select(MaintenanceJob).where(MaintenanceJob.kind == 'update.download'))
        with pytest.raises(UpdateFailure):
            service.run_download(db, job)
        assert get_update_state(db).downloaded_at is None
    assert not list(tmp_path.glob('*.tar')) and not list(tmp_path.glob('*.part'))


def test_admission_fence_survives_business_commit_and_blocks_new_writes(client):
    headers = login(client)
    admission = request_admission(SimpleNamespace(method='POST', url=SimpleNamespace(path='/api/v1/me/preferences')))
    next(admission)
    with Session(engine()) as business:
        business.execute(text('SELECT 1'))
        business.commit()
    with engine().connect() as guard:
        assert not guard.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': KEY})
    with pytest.raises(StopIteration):
        next(admission)
    with engine().connect() as guard:
        assert guard.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': KEY})
    with Session(engine()) as db, db.begin():
        get_system_state(db).active = True
    assert client.get('/api/v1/me/preferences').status_code == 200
    blocked = client.put('/api/v1/me/preferences', json={'start_page': 'review', 'review_lookback': 20, 'news_category': '精选', 'expected_revision': 1}, headers=key(headers, 'blocked-writer'))
    assert blocked.status_code == 503 and blocked.json()['error']['code'] == 'maintenance_active'
    from app.worker import claim
    from app.ai.worker import claim as ai_claim
    assert claim() is None and ai_claim() is None


def test_orphan_claim_after_prejournal_interruption(client, signed):
    login(client)
    from app.models import User
    with Session(engine(), expire_on_commit=False) as db:
        owner = db.scalar(select(User))
        job = MaintenanceJob(owner_id=owner.id, kind='update.install', status='running', stage='preflight',
            params={'manifest': signed.document, 'signature': signed.signature, 'confirmed': True, 'drafts_saved': True},
            lease_until=datetime.now(UTC) - timedelta(seconds=1))
        db.add(job)
        db.commit()
        resumed = bridge.dispatch(db, {'action': 'claim'})
        assert resumed['id'] == str(job.id) and resumed['token']


class FakeCompose:
    def __init__(self, path, *, failure=None, incompatible=False):
        self.work = path
        self.progress = path / 'public'
        self.progress.mkdir()
        self.config, self.control_config = path / 'deployment.private.json', path / 'control.private.json'
        atomic_json(self.config, {'original': True})
        self.failure, self.calls, self.restored = failure, [], False
        schema = 'next-incompatible' if incompatible else SCHEMA_REVISION
        self.job = {'id': 'test-job', 'token': 'test-token', 'compatible_revisions': [SCHEMA_REVISION],
            'params': {'release_id': 'fixture', 'manifest': {'version': '0.4.1', 'schema': {'to_revision': schema, 'backward_compatible': not incompatible}}}}
        self.claimed, self.finished = False, None

    def bridge(self, action, job=None, **values):
        self.calls.append(action)
        if action == 'claim':
            if self.claimed:
                return None
            self.claimed = True
            return self.job
        if action == 'describe':
            return {'running': 1 if self.failure == 'drain' else 0}
        if action == 'backup':
            return {'backup_id': 'complete-backup'}
        if action == 'finish':
            self.finished = values
        if action == 'observe':
            return self.finished or {'status': 'running'}
        return {'ok': True}

    def step(self, phase):
        self.calls.append(phase)
        if self.failure == phase:
            raise UpgradeError('fixture_' + phase, 'Injected isolated failure')

    def preflight(self, job):
        self.step('preflight')
        return self.work / 'fixture.tar'
    def load(self, package, job): self.step('load')
    def stop(self): self.step('stop')
    def switch(self, job): self.step('switch')
    def migrate(self): self.step('migrate')
    def start(self): self.step('start')
    def verify(self, job): self.step('verify')
    def restore_program(self, before):
        assert json.loads(before.read_text()) == {'original': True}
        self.restored = True
        self.step('restore')


@pytest.mark.parametrize('failure', [None, 'preflight', 'stop', 'switch', 'migrate', 'start', 'verify', 'drain'])
def test_host_normal_and_failure_paths(tmp_path, failure):
    adapter = FakeCompose(tmp_path, failure=failure)
    updater = Updater(adapter, drain_seconds=0, wait=lambda _: None)
    assert updater.once()
    assert adapter.finished['status'] == ('succeeded' if failure is None else 'failed')
    assert adapter.restored == (failure in {'stop', 'switch', 'migrate', 'start', 'verify'})
    if failure not in {'preflight', 'drain'}:
        assert 'backup' in adapter.calls
    assert json.loads((adapter.progress / 'status.json').read_text())['terminal']


def test_host_incompatible_migration_failure_preserves_current_database(tmp_path):
    adapter = FakeCompose(tmp_path, failure='migrate', incompatible=True)
    Updater(adapter).once()
    assert adapter.finished['status'] == 'manual_recovery' and not adapter.restored
    progress = json.loads((adapter.progress / 'status.json').read_text())
    assert progress['backup_available'] and progress['error_code']


@pytest.mark.parametrize('incompatible', [False, True])
def test_host_restart_from_migration(tmp_path, incompatible):
    adapter = FakeCompose(tmp_path, incompatible=incompatible)
    updater = Updater(adapter)
    atomic_json(updater.before, {'original': True})
    atomic_json(updater.journal, {'job': adapter.job, 'phase': 'migrating', 'backup': {'backup_id': 'complete-backup'}})
    updater.once()
    assert adapter.finished['status'] == ('manual_recovery' if incompatible else 'failed')
    assert adapter.restored is not incompatible


def test_host_restart_after_committed_success_only_repairs_progress(tmp_path):
    adapter = FakeCompose(tmp_path)
    updater = Updater(adapter)
    adapter.finished = {'status': 'succeeded', 'message': 'Completed before the host interrupted', 'error_code': None}
    atomic_json(updater.journal, {'job': adapter.job, 'phase': 'verifying', 'backup': {'backup_id': 'complete-backup'}})
    updater.once()
    assert not adapter.restored and 'claim' not in adapter.calls
    assert json.loads(updater.journal.read_text())['phase'] == 'succeeded'


def test_host_status_write_fails_after_success_never_rolls_back_opened_writes(tmp_path, monkeypatch):
    adapter = FakeCompose(tmp_path)
    updater = Updater(adapter)
    original = updater.publish
    failed = False
    def interrupted(job, phase, message, **values):
        nonlocal failed
        if phase == 'succeeded' and not failed:
            failed = True
            raise OSError('Injected status write interruption after committed success')
        return original(job, phase, message, **values)
    monkeypatch.setattr(updater, 'publish', interrupted)
    updater.once()
    assert adapter.finished['status'] == 'succeeded' and not adapter.restored
    assert json.loads(updater.journal.read_text())['phase'] == 'succeeded'


def test_bridge_finish_is_idempotent_and_revoked_owner_cannot_install(client, signed):
    login(client)
    from app.models import User
    import uuid
    with Session(engine(), expire_on_commit=False) as db:
        owner = db.scalar(select(User))
        job = MaintenanceJob(owner_id=owner.id, kind='update.install', status='running', stage='verifying',
            params={'release_id': signed.document['id']}, lease_token=uuid.uuid4())
        db.add(job)
        db.commit()
        command = {'action': 'finish', 'id': str(job.id), 'token': str(job.lease_token), 'status': 'succeeded',
                   'message': 'Isolated finish replay', 'result': {}}
        assert bridge.dispatch(db, command) == {'ok': True}
        assert bridge.dispatch(db, command) == {'ok': True}
        owner.enabled = False
        revoked = MaintenanceJob(owner_id=owner.id, kind='update.install', status='queued',
            params={'manifest': signed.document, 'signature': signed.signature, 'confirmed': True, 'drafts_saved': True})
        db.add(revoked)
        db.commit()
        assert bridge.dispatch(db, {'action': 'claim'}) is None
        db.refresh(revoked)
        assert revoked.status == 'failed' and revoked.error_code == 'maintenance_permission_changed'


def test_low_space_download_publishes_no_success(client, signed, monkeypatch, tmp_path):
    headers = login(client)
    client.post('/api/v1/system/updates/check', headers=key(headers, 'check-low-space'))
    client.post('/api/v1/system/updates/download', json={'release_id': signed.document['id']}, headers=key(headers, 'download-low-space'))
    monkeypatch.setattr(service.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    with Session(engine()) as db:
        job = db.scalar(select(MaintenanceJob).where(MaintenanceJob.kind == 'update.download'))
        with pytest.raises(UpdateFailure, match='空间不足'):
            service.run_download(db, job)
        assert get_update_state(db).downloaded_at is None
    assert not list(tmp_path.iterdir())
