"""Run separately from schema 0029 on the disposable PostgreSQL instance."""
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
import pytest

from app.db import engine
from app.evidence.models import EvidenceVersion
from test_ths_theme_index import seed


def test_existing_member_pages_upgrade_without_changing_evidence_or_other_facets(client, monkeypatch):
    with engine().connect() as connection:
        current = connection.scalar(text('SELECT version_num FROM alembic_version'))
    if current in {'0030_ths_member_capture_facet', '0031_collection_gate_indexes'}:
        pytest.skip('Upgrade rehearsal runs separately from a nonempty schema 0029 database.')
    assert current == '0029_focused_quote_cadence'
    seed(client, monkeypatch)
    with Session(engine()) as db:
        originals = [(row.id, row.content_hash, row.payload) for row in db.scalars(
            select(EvidenceVersion).where(EvidenceVersion.kind == 'reference'))]
        before = dict(db.execute(text('SELECT evidence_id, to_jsonb(f) FROM evidence_read_facets f')).all())
    command.upgrade(Config('alembic.ini'), 'head')
    with Session(engine()) as db:
        assert db.scalar(text('SELECT version_num FROM alembic_version')) == '0030_ths_member_capture_facet'
        after = dict(db.execute(text("SELECT evidence_id, to_jsonb(f)-'ths_member_capture_id' FROM evidence_read_facets f")).all())
        assert before == after
        for ident, digest, payload in originals:
            row = db.get(EvidenceVersion, ident)
            assert row.content_hash == digest and row.payload == payload
            actual = db.scalar(text('SELECT ths_member_capture_id FROM evidence_read_facets WHERE evidence_id=:ident'), {'ident': ident})
            expected = payload['rows'][0]['capture_id'] if payload['dataset'] == 'tushare.ths_member' else None
            assert actual == expected
    # Expansion retains SELECT-only privileges for the application role.
    app = create_engine(os.environ['JL_TEST_APP_URL'])
    try:
        with app.connect() as connection:
            assert connection.scalar(text('SELECT count(ths_member_capture_id) FROM evidence_read_facets')) == 2
            with pytest.raises(DBAPIError):
                connection.execute(text("UPDATE evidence_read_facets SET ths_member_capture_id='forged'"))
            connection.rollback()
    finally:
        app.dispose()
