"""Operator activation through the real collector, on disposable PostgreSQL."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.models import Connection
from app.auth import APIError
from app.config import settings
from app.db import engine
from app.evidence.models import SourceVersion
from app.market.enable_sector_atlas import run
from app.market.models import SourceSettings
from app.market.protocol import Response
from app.market.registry import SOURCES
from app.market.service import source_id
from app.models import AuditEvent, Job, Provider
from app.worker import claim, process
from test_data_sources import configure, configuration
from test_integration_data import connection, PRIVATE
from test_reference_backfill import setup as reference_setup
from test_tushare import source


def snapshot(row, exclude=()):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns if c.name not in exclude}


def setup(client, monkeypatch, tmp_path):
    headers, day = reference_setup(client, monkeypatch, 'tushare_ths_index')
    configure(client, headers, source('tushare_limit_list_ths'), key='existing-limit-details')
    configure(client, headers, configuration('direction_kpl_strength'), key='existing-kpl')
    master = tmp_path / 'master'
    master.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(settings(), 'ai_master_key_file', str(master))
    monkeypatch.setattr(settings(), 'ai_secret_directory', str(tmp_path / 'secrets'))
    from app.ai import vault
    read_secret = vault.read_secret
    monkeypatch.setattr(vault, 'read_secret', lambda ref: PRIVATE if ref == 'vault:synthetic' else read_secret(ref))
    with Session(engine()) as db, db.begin():
        old = connection(db, ('tushare_ths_index',))
        group = connection(db, ('tushare_limit_list_ths',))
        kpl = db.get(Provider, source_id('direction_kpl_strength'))
        kpl.adapter_version = 'previous-synthetic-version'
        kpl.enabled = False
        config = db.get(SourceSettings, kpl.id)
        config.paused_reason = 'manual-test-hold'
        before = dict(connections={str(c.id): snapshot(c) for c in (old, group)},
                      kpl=snapshot(kpl, ('adapter_version', 'capabilities')), config=snapshot(config))
    return headers, day, before


@pytest.mark.parametrize('wrong_day', [False, True])
def test_prepare_real_collection_then_finish_only_enables_valid_probe(client, monkeypatch, tmp_path, wrong_day):
    _, day, before = setup(client, monkeypatch, tmp_path)
    calls = []
    def vendor(api, parameters, fields, credential, endpoint):
        calls.append((api, parameters, credential.get_secret_value()))
        row = dict(ts_code='885001.TI', trade_date=(day-timedelta(days=int(wrong_day))).strftime('%Y%m%d'),
                   close='101', open='100', high='102', low='99', pre_close='100', avg_price='100',
                   change='1', pct_change='1', vol='100', turnover_rate='2')
        body = json.dumps({'code': 0, 'data': {'fields': list(row), 'items': [list(row.values())]}}).encode()
        return Response(body, datetime.now(UTC), 1, 200)
    monkeypatch.setattr('app.market.collect.request_tushare', vendor)
    prepared = run('prepare', day)
    assert prepared['enabled'] is False
    pending = run('finish', day)
    assert pending['enabled'] is False and pending['probe']['status'] == 'queued'
    job = claim()
    assert str(job.id) == prepared['job_id']
    process(job)
    result = run('finish', day)
    assert result['enabled'] is (not wrong_day)
    assert len(calls) == 1 and calls[0][0] == 'ths_daily'
    assert calls[0][1]['trade_date'] == day.strftime('%Y%m%d') and calls[0][2] == PRIVATE
    with Session(engine()) as db:
        current = db.get(Job, uuid.UUID(prepared['job_id']))
        assert current.state == ('failed' if wrong_day else 'succeeded')
        for identity, value in before['connections'].items():
            assert snapshot(db.get(Connection, uuid.UUID(identity))) == value
        kpl = db.get(Provider, source_id('direction_kpl_strength'))
        assert snapshot(kpl, ('adapter_version', 'capabilities')) == before['kpl']
        assert snapshot(db.get(SourceSettings, kpl.id)) == before['config']
        assert kpl.adapter_version == SOURCES[kpl.family].version
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.action == 'source.atlas_upgrade')) is not None
        assert db.scalar(select(SourceVersion.id).where(SourceVersion.source_id == kpl.id,
            SourceVersion.descriptor['adapter_version'].astext == kpl.adapter_version)) is not None
        added = [c for c in db.scalars(select(Connection)) if str(c.id) not in before['connections']]
        assert len(added) == 1 and added[0].enabled is (not wrong_day)
        assert added[0].source_ids == [str(source_id('tushare_ths_daily'))]
        if not wrong_day:
            assert added[0].tested_revision == added[0].revision


def test_prepare_respects_existing_disabled_daily_source_and_rolls_back_upgrade(client, monkeypatch, tmp_path):
    headers, day, before = setup(client, monkeypatch, tmp_path)
    body = source('tushare_ths_daily')
    body['enabled'] = False
    configure(client, headers, body, key='disabled-daily')
    with pytest.raises(APIError):
        run('prepare', day)
    with Session(engine()) as db:
        assert db.get(Provider, source_id('tushare_ths_daily')).enabled is False
        assert db.get(Provider, source_id('direction_kpl_strength')).adapter_version == 'previous-synthetic-version'
        assert {str(c.id) for c in db.scalars(select(Connection))} == set(before['connections'])
        assert db.scalar(select(Job.id)) is None
