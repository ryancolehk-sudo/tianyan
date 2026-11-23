"""First-run privileges on disposable PostgreSQL; never binds a real port."""
import json

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import User
from app.native.config import NativeConfig, atomic_json, prepare_directories
from app.native.web import create_app


def native_client(tmp_path, monkeypatch):
    program = tmp_path / 'program'
    program.mkdir()
    (program / 'release.json').write_text(json.dumps({'build_revision': 'a' * 40}))
    (program / 'program-manifest.json').write_text('{}')
    config_file = tmp_path / 'runtime.json'
    atomic_json(config_file, {'format': 1, 'program': str(program), 'data': str(tmp_path / 'data'),
                             'http_port': 18048, 'database_port': 55448})
    config = NativeConfig.read(config_file)
    prepare_directories(config)
    monkeypatch.setenv('JL_NATIVE_CONFIG_FILE', str(config_file))
    return TestClient(create_app(), base_url=config.origin), config


def test_first_admin_requires_one_time_token_same_origin_and_fixed_host(tmp_path, monkeypatch):
    client, config = native_client(tmp_path, monkeypatch)
    body = {'username': 'native-admin', 'password': 'fixture-password-only-123'}
    with client:
        assert client.get('/native/setup/status').json() == {'setup_required': True}
        assert client.post('/native/setup', json=body, headers={'origin': config.origin}).status_code == 403
        token = config.private()['setup_token']
        headers = {'origin': 'http://another-origin.invalid', 'x-tianyan-setup': token}
        assert client.post('/native/setup', json=body, headers=headers).status_code == 403
        headers['origin'] = config.origin
        assert client.post('/native/setup', json=body, headers={**headers, 'host': 'attacker.invalid'}).status_code == 403
        assert client.post('/native/setup', json=body, headers=headers).status_code == 200
        assert client.get('/native/setup/status').json() == {'setup_required': False}
        assert client.post('/native/setup', json=body, headers=headers).status_code == 403
    with Session(engine()) as db:
        users = list(db.scalars(select(User)))
        assert len(users) == 1 and users[0].role == 'admin'
        assert users[0].password_hash != body['password']
    assert 'setup_token' not in config.private()


def test_setup_cannot_create_second_user_after_existing_database_restore(tmp_path, monkeypatch):
    from app.auth import hasher
    with Session(engine()) as db, db.begin():
        db.add(User(username='restored-admin', password_hash=hasher.hash('fixture-existing-password'), role='admin', enabled=True))
    client, config = native_client(tmp_path, monkeypatch)
    with client:
        response = client.post('/native/setup', json={'username': 'new-admin', 'password': 'fixture-password-only-123'},
            headers={'origin': config.origin, 'x-tianyan-setup': config.private()['setup_token']})
        assert response.status_code == 409
    with Session(engine()) as db:
        assert [row.username for row in db.scalars(select(User))] == ['restored-admin']
