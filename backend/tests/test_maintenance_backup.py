"""Only disposable PostgreSQL and private temporary files; no source/API calls."""
import json
import os
import tarfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.ai import vault
from app.ai.models import Connection
from app.auth import hasher
from app.chatgpt import service as chatgpt
from app.config import settings
from app.db import engine
from app.maintenance import backup, worker
from app.models import AuditEvent, User
from app.system_settings.models import BackupRecord, MaintenanceJob, SystemState
from app.system_settings.service import create_job


@pytest.fixture
def sources(tmp_path, monkeypatch):
    credentials, artifacts, destination = (tmp_path / name for name in ('credentials', 'artifacts', 'backups'))
    credentials.mkdir()
    artifacts.mkdir()
    (artifacts / 'research.txt').write_text('isolated research bytes', encoding='utf-8')
    master = tmp_path / 'master'
    master.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(settings(), 'ai_secret_directory', str(credentials))
    monkeypatch.setattr(settings(), 'ai_master_key_file', str(master))
    monkeypatch.setattr(settings(), 'backup_directory', str(destination))
    original = backup.create_backup
    def create(identity, **kwargs):
        return original(identity, artifact_directory=artifacts, **kwargs)
    monkeypatch.setattr(worker, 'create_backup', create)
    return credentials, artifacts, master, destination


def owner():
    with Session(engine()) as db, db.begin():
        user = User(id=uuid.uuid4(), username='backup-admin', role='admin',
                    password_hash=hasher.hash('isolated-backup-password'), enabled=True)
        db.add(user)
        return user.id


def queue(kind='backup'):
    identity = owner()
    with Session(engine(), expire_on_commit=False) as db:
        job = create_job(db, identity, kind)
        db.commit()
        return job.id


def fake_dump(monkeypatch, callback=None):
    def perform(snapshot, destination, database_url):
        if callback:
            callback(snapshot)
        destination.write_bytes(b'isolated database payload')
    monkeypatch.setattr(backup, '_dump', perform)


def create(sources, identity=None):
    return backup.create_backup(identity or uuid.uuid4(), artifact_directory=sources[1])


def test_format_two_manifest_last_and_separate_master(sources, monkeypatch):
    fake_dump(monkeypatch)
    steps = []
    result = backup.create_backup(uuid.uuid4(), artifact_directory=sources[1], progress=lambda stage, _: steps.append(stage))
    folder = backup.location(result['location_key'])
    manifest = backup.verify_bundle(folder)
    assert manifest['format'] == 2 and set(manifest['sha256']) == set(backup.FILES)
    assert result['integrity_state'] == 'passed' and result['recovery_state'] == 'unverified'
    assert (folder.with_name(folder.name + '-master-key') / 'ai_master_key').read_bytes() == sources[2].read_bytes()
    assert not (folder / 'ai_master_key').exists()
    assert steps == ['checking', 'packing_credentials', 'exporting', 'packing', 'verifying']


def test_result_reuses_completed_same_job_after_process_loss(sources, monkeypatch):
    fake_dump(monkeypatch)
    identity = uuid.uuid4()
    first = create(sources, identity)
    monkeypatch.setattr(backup, '_dump', lambda *_: pytest.fail('published backup must not export twice'))
    assert create(sources, identity) == first


def test_interrupted_staging_restarts_without_publishing_incomplete(sources, monkeypatch):
    identity = uuid.uuid4()
    target = backup.location('backup-' + str(identity))
    work = target.with_name('.' + target.name + '.partial')
    work.mkdir()
    (work / 'database.dump').write_bytes(b'partial')
    fake_dump(monkeypatch)
    create(sources, identity)
    assert backup.verify_bundle(target)['format'] == 2
    assert not work.exists()


def test_export_failure_never_publishes_manifest(sources, monkeypatch):
    def fail(*_):
        raise backup.BackupFailure('backup_export_failed', 'isolated failure')
    monkeypatch.setattr(backup, '_dump', fail)
    with pytest.raises(backup.BackupFailure, match='isolated failure'):
        create(sources)
    assert not list(sources[3].glob('backup-*/manifest.json'))


def test_enospc_has_controlled_failure_and_no_published_bundle(sources, monkeypatch):
    def fail(*_):
        raise OSError(28, 'synthetic disk full')
    monkeypatch.setattr(backup, '_pack', fail)
    with pytest.raises(backup.BackupFailure) as failure:
        create(sources)
    assert failure.value.code == 'backup_space_insufficient'
    assert not list(sources[3].glob('backup-*/manifest.json'))
    with engine().connect() as connection:
        assert connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': vault.CREDENTIAL_GATE})
        connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': vault.CREDENTIAL_GATE})


@pytest.mark.parametrize('name', backup.FILES)
def test_tamper_is_rejected(sources, monkeypatch, name):
    fake_dump(monkeypatch)
    result = create(sources)
    folder = backup.location(result['location_key'])
    with (folder / name).open('ab') as output:
        output.write(b'changed')
    with pytest.raises(backup.BackupFailure):
        backup.verify_bundle(folder)


def test_mismatched_master_is_rejected(sources, monkeypatch):
    fake_dump(monkeypatch)
    result = create(sources)
    folder = backup.location(result['location_key'])
    key = folder.with_name(folder.name + '-master-key') / 'ai_master_key'
    key.chmod(0o600)
    key.write_bytes(Fernet.generate_key())
    with pytest.raises(backup.BackupFailure):
        backup.verify_bundle(folder)


def test_missing_referenced_ciphertext_blocks_backup(sources, monkeypatch):
    fake_dump(monkeypatch)
    with Session(engine()) as db, db.begin():
        db.add(Connection(name='offline', kind='model', template='custom_model', endpoint='https://example.invalid',
            options={}, source_ids=[], secret_ref='vault:' + str(uuid.uuid4())))
    with pytest.raises(backup.BackupFailure) as failure:
        create(sources)
    assert failure.value.code == 'backup_credentials_incomplete'


def test_symlink_artifact_is_rejected(sources, monkeypatch):
    fake_dump(monkeypatch)
    (sources[1] / 'unsafe').symlink_to(sources[2])
    with pytest.raises(backup.BackupFailure) as failure:
        create(sources)
    assert failure.value.code == 'backup_source_invalid'


@pytest.mark.parametrize('value', ['../data', 'backup-../../x', 'backup-nope'])
def test_location_must_be_fixed_job_uuid(sources, value):
    with pytest.raises(backup.BackupFailure):
        backup.location(value)


def test_writer_gate_lives_until_db_commit(sources):
    with Session(engine()) as writer:
        vault.write_secret('offline-key', db=writer)
        with engine().connect() as check:
            assert not check.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': vault.CREDENTIAL_GATE})
        writer.commit()
        with engine().connect() as check:
            assert check.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': vault.CREDENTIAL_GATE})
            check.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': vault.CREDENTIAL_GATE})


def test_backup_waits_for_credential_file_and_reference_commit(sources, monkeypatch):
    fake_dump(monkeypatch)
    started = threading.Event()
    original = backup.frozen_credentials
    def enter(bind):
        started.set()
        return original(bind)
    monkeypatch.setattr(backup, 'frozen_credentials', enter)
    with Session(engine()) as writer, ThreadPoolExecutor(max_workers=1) as pool:
        reference = vault.write_secret('new-committed-key', db=writer)
        writer.add(Connection(name='saved', kind='model', template='custom_model', endpoint='https://example.invalid',
                              secret_ref=reference, options={}, source_ids=[]))
        result = pool.submit(create, sources)
        assert started.wait(3) and not result.done()
        writer.commit()
        saved = result.result(timeout=5)
    folder = backup.location(saved['location_key'])
    with tarfile.open(folder / 'credentials.tar') as archive:
        raw = archive.extractfile('credentials/' + reference[6:] + '.key').read()
        assert Fernet(sources[2].read_bytes()).decrypt(raw) == b'new-committed-key'


def test_snapshot_and_tokens_stay_paired_when_rotation_continues_during_dump(sources, monkeypatch):
    with Session(engine()) as db, db.begin():
        reference = chatgpt.save_bundle({'token': 'before'}, db=db)
        connection = Connection(id=uuid.uuid4(), name='saved', kind='model', template='custom_model',
            endpoint='https://example.invalid', secret_ref=reference, options={'generation': 'before'}, source_ids=[])
        db.add(connection)
        identity = connection.id
    def during_dump(snapshot):
        # The exclusive backup gate has already been released here. A complete
        # file replacement + DB commit must proceed even while pg_dump runs.
        with Session(engine()) as writer, writer.begin():
            chatgpt.save_bundle({'token': 'after'}, reference, db=writer)
            writer.get(Connection, identity).options = {'generation': 'after'}
        with engine().connect().execution_options(isolation_level='REPEATABLE READ') as frozen, frozen.begin():
            assert all(c in '0123456789ABCDEF-' for c in snapshot)
            frozen.execute(text("SET TRANSACTION SNAPSHOT '" + snapshot + "'"))
            assert frozen.scalar(select(Connection.options).where(Connection.id == identity)) == {'generation': 'before'}
    fake_dump(monkeypatch, during_dump)
    result = create(sources)
    folder = backup.location(result['location_key'])
    with tarfile.open(folder / 'credentials.tar') as archive:
        raw = archive.extractfile('credentials/' + reference[6:] + '.key').read()
    assert json.loads(Fernet(sources[2].read_bytes()).decrypt(raw)) == {'token': 'before'}
    assert json.loads(vault.read_secret(reference)) == {'token': 'after'}


def test_worker_records_integrity_without_claiming_recovery(sources, monkeypatch):
    fake_dump(monkeypatch)
    identity = queue()
    with Session(engine(), expire_on_commit=False) as db:
        assert worker.run_one(db)
        job = db.get(MaintenanceJob, identity)
        record = db.scalar(select(BackupRecord).where(BackupRecord.job_id == identity))
        assert job.status == 'succeeded' and record.integrity_state == 'passed'
        assert record.recovery_state == 'unverified' and record.recovery_verified_at is None
        assert job.result['backup_id'] == str(record.id)
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'maintenance.backup_completed'))


def test_worker_failure_preserves_failed_history(sources, monkeypatch):
    def fail(*_, **__):
        raise backup.BackupFailure('backup_space_insufficient', '本机空间不足。')
    monkeypatch.setattr(worker, 'create_backup', fail)
    identity = queue()
    with Session(engine(), expire_on_commit=False) as db:
        assert worker.run_one(db)
        job = db.get(MaintenanceJob, identity)
        record = db.scalar(select(BackupRecord).where(BackupRecord.job_id == identity))
        assert job.status == 'failed' and job.error_code == 'backup_space_insufficient'
        assert record.integrity_state == 'failed' and record.recovery_state == 'unverified'
        assert not list(sources[3].glob('backup-*/manifest.json'))


def test_worker_never_claims_update_install(sources, monkeypatch):
    identity = queue('update.install')
    with Session(engine(), expire_on_commit=False) as db:
        assert not worker.run_one(db)
        assert db.get(MaintenanceJob, identity).status == 'queued'


def test_worker_stops_claiming_during_upgrade(sources):
    identity = queue()
    with Session(engine(), expire_on_commit=False) as db:
        db.add(SystemState(id=1, active=True, stage='draining'))
        db.commit()
        assert not worker.run_one(db)
        assert db.get(MaintenanceJob, identity).status == 'queued'


def test_worker_rechecks_administrator_before_export(sources, monkeypatch):
    identity = queue()
    monkeypatch.setattr(worker, 'create_backup', lambda *_, **__: pytest.fail('revoked admin must not export'))
    with Session(engine(), expire_on_commit=False) as db:
        job = db.get(MaintenanceJob, identity)
        db.get(User, job.owner_id).role = 'viewer'
        db.commit()
        assert worker.run_one(db)
        db.refresh(job)
        assert job.status == 'failed' and job.error_code == 'maintenance_permission_changed'


def test_expired_lease_resumes_same_backup(sources, monkeypatch):
    fake_dump(monkeypatch)
    identity = queue()
    with Session(engine(), expire_on_commit=False) as db:
        job = db.get(MaintenanceJob, identity)
        job.status, job.lease_token, job.lease_until = 'running', uuid.uuid4(), datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        assert worker.run_one(db)
        assert db.get(MaintenanceJob, identity).status == 'succeeded'
        assert len(list(sources[3].glob('backup-*/manifest.json'))) == 1


def test_dedicated_role_has_business_read_only_and_maintenance_write(sources):
    url = os.getenv('JL_TEST_MAINTENANCE_URL')
    if not url:
        pytest.skip('Dedicated role DSN is supplied by disposable Compose checks')
    restricted = create_engine(url)
    try:
        with restricted.begin() as db:
            assert db.scalar(text('SELECT count(*) FROM users')) == 0
        with pytest.raises(ProgrammingError):
            with restricted.begin() as db:
                db.execute(text("UPDATE providers SET name=name"))
        with restricted.begin() as db:
            db.execute(text("UPDATE system_settings_state SET backup_heartbeat_at=now()"))
    finally:
        restricted.dispose()
