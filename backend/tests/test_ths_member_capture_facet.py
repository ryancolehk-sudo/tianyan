"""Small derived capture metadata preserves the authoritative page selection."""
from datetime import timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import read_facets
from app.market.read_facets import EvidenceReadFacet
from test_terminal_workbench import AT
from test_ths_theme_index import projected, seed


def test_member_capture_metadata_is_atomic_and_does_not_rewrite_originals(client, monkeypatch):
    seed(client, monkeypatch)
    with Session(engine()) as db:
        pages = list(db.scalars(select(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext == 'tushare.ths_member')))
        assert len(pages) == 2
        before = [(row.id, row.content_hash, row.payload) for row in pages]
        for page in pages:
            facet = db.get(EvidenceReadFacet, page.id)
            assert facet.ths_member_capture_id == page.payload['rows'][0]['capture_id']
            assert facet.content_hash == page.content_hash
        # This derived field has an explicit member-page scope. Other datasets
        # cannot acquire its meaning just because a row has a capture_id key.
        for original in db.scalars(select(EvidenceVersion).where(
                EvidenceVersion.kind == 'reference',
                EvidenceVersion.payload['dataset'].astext != 'tushare.ths_member')):
            assert db.get(EvidenceReadFacet, original.id).ths_member_capture_id is None
        db.expire_all()
        assert [(db.get(EvidenceVersion, ident).id,
                 db.get(EvidenceVersion, ident).content_hash,
                 db.get(EvidenceVersion, ident).payload)
                for ident, _, _ in before] == before


@pytest.mark.parametrize('facet_state', ['present', 'missing', 'stale'])
def test_cold_page_selection_matches_original_when_facets_are_unavailable(
        client, monkeypatch, facet_state):
    seed(client, monkeypatch)
    monkeypatch.setattr(settings(), 'read_cache_enabled', False)
    expected, expected_refs = projected(codes=('000001.SZ', '000002.SZ', '000003.SZ'))
    with Session(engine()) as db, db.begin():
        if facet_state == 'missing':
            db.execute(delete(EvidenceReadFacet))
        elif facet_state == 'stale':
            for facet in db.scalars(select(EvidenceReadFacet)):
                facet.content_hash = '0' * 64
    actual, actual_refs = projected(codes=('000001.SZ', '000002.SZ', '000003.SZ'))
    assert {code: vars(row) for code, row in actual.items()} == {
        code: vars(row) for code, row in expected.items()}
    assert actual_refs == expected_refs


def test_capture_projection_never_uses_a_stale_hash(
        client, monkeypatch):
    seed(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        page = db.scalar(select(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext == 'tushare.ths_member'))
        ident = page.id
        expected = page.payload['rows'][0]['capture_id']
        facet = db.get(EvidenceReadFacet, ident)
        facet.content_hash = '0' * 64
        facet.ths_member_capture_id = 'stale-capture-must-not-be-used'
        db.flush()
        query = read_facets.with_facets(select(read_facets.value('ths_member_capture_id'))
            .select_from(EvidenceVersion)).where(EvidenceVersion.id == ident)
        assert db.scalar(query) == expected


@pytest.mark.parametrize('dataset,rows,expected', [
    ('tushare.ths_member', [], None),
    ('tushare.ths_member', [{'capture_id': None}, {'capture_id': 'second'}], None),
    ('tushare.ths_member', [{'other': 'first'}, {'other': 'second'}], None),
    ('tushare.ths_member', [{'capture_id': 'first'}, {'capture_id': 'second'}], 'first'),
    ('other.reference', [{'capture_id': 'other'}], None),
])
def test_capture_projection_preserves_nulls_first_row_and_dataset_scope(
        client, monkeypatch, dataset, rows, expected):
    from app.evidence.schemas import ReferencePayload
    from app.market.collect import store_reference_batch
    from app.market.models import SourceSettings
    from app.market.reference import ReferenceBatch
    from app.market.service import source_id
    from app.models import Provider
    seed(client, monkeypatch)
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('tushare_ths_member'))
        payload = ReferencePayload(dataset=dataset, title='synthetic capture metadata',
            rows=rows, columns=[{'key': key, 'label': key} for key in (rows[0] if rows else {'capture_id': None})],
            source_url='https://example.invalid/synthetic',
            attribution='synthetic fixture', scope='synthetic fixture')
        store_reference_batch(db, provider, db.get(SourceSettings, provider.id),
            ReferenceBatch(records=[('synthetic-capture', payload, None)], expected=1), AT)
        original = db.scalar(select(EvidenceVersion).where(
            EvidenceVersion.source_key == 'reference:synthetic-capture'))
        ident = original.id
        assert db.get(EvidenceReadFacet, ident).ths_member_capture_id == expected
        for state in ('present', 'stale', 'missing'):
            if state == 'stale':
                db.get(EvidenceReadFacet, ident).content_hash = '0' * 64
                db.flush()
            if state == 'missing':
                db.execute(delete(EvidenceReadFacet).where(EvidenceReadFacet.evidence_id == ident))
            query = read_facets.with_facets(select(read_facets.value('ths_member_capture_id'))
                .select_from(EvidenceVersion)).where(EvidenceVersion.id == ident)
            assert db.scalar(query) == expected


def test_later_extra_page_still_rejects_a_capture_after_cold_restart(client, monkeypatch):
    batch = seed(client, monkeypatch)
    from app.market.collect import store_reference_batch
    from app.market.models import SourceSettings
    from app.market.reference import ReferenceBatch
    from app.market.service import source_id
    from app.models import Provider
    later = AT + timedelta(minutes=1)
    extra = batch.records[0][1].model_copy(update={
        'rows': [dict(batch.records[0][1].rows[0], con_code='000003.SZ')]})
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('tushare_ths_member'))
        store_reference_batch(db, provider, db.get(SourceSettings, provider.id),
            ReferenceBatch(records=[('ths_member:page:extra', extra, None)], expected=1), later)
    monkeypatch.setattr(settings(), 'read_cache_enabled', False)
    assert projected()[0]['000001.SZ'].ths_themes
    assert not projected(later)[0]['000001.SZ'].ths_themes
