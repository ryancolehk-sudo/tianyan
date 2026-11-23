"""Older role-specific modules can keep using the expanded schema safely."""
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.evidence.schemas import ReferencePayload
from app.market.collect import store_reference_batch
from app.market.models import SourceSettings
from app.market.read_facets import EvidenceReadFacet
from app.market.reference import ReferenceBatch
from app.market.service import source_id
from app.models import Provider
from app.version import SCHEMA_COMPATIBLE_REVISIONS, SCHEMA_REVISION
from test_terminal_workbench import AT, setup


def test_role_can_read_existing_facets_and_append_atomic_member_records(client, monkeypatch):
    assert SCHEMA_REVISION == '0031_collection_gate_indexes'
    assert SCHEMA_COMPATIBLE_REVISIONS == frozenset({SCHEMA_REVISION})
    setup(client, monkeypatch, ['tushare_ths_member'])
    # Keep the compatibility fixture independent of newer collector helpers:
    # scheduler/AI/maintenance roles do not execute those collection paths.
    with Session(engine()) as db, db.begin():
        provider = db.get(Provider, source_id('tushare_ths_member'))
        records = []
        for index in range(2):
            row = dict(ts_code='885800.TI', con_code=f'{index+1:06}.SZ', capture_id='synthetic-capture')
            payload = ReferencePayload(dataset='tushare.ths_member', title='synthetic compatibility',
                rows=[row], columns=[{'key': key, 'label': key} for key in row],
                source_url='https://example.invalid/synthetic',
                attribution='synthetic fixture', scope='synthetic fixture')
            records.append((f'synthetic-member:{index}', payload, None))
        store_reference_batch(db, provider, db.get(SourceSettings, provider.id),
            ReferenceBatch(records=records, expected=2), AT)
    with Session(engine()) as db:
        pages = list(db.scalars(select(EvidenceVersion).where(
            EvidenceVersion.payload['dataset'].astext == 'tushare.ths_member')))
        assert len(pages) == 2
        for page in pages:
            # A role that retains the older ORM selects only its known fields;
            # the expanded trigger still records the new field atomically.
            facet = db.get(EvidenceReadFacet, page.id)
            assert facet.content_hash == page.content_hash
            assert facet.dataset == page.payload['dataset']
            capture = db.scalar(text('SELECT ths_member_capture_id FROM evidence_read_facets WHERE evidence_id=:ident'), {'ident': page.id})
            assert capture == page.payload['rows'][0]['capture_id']
    assert client.get('/health/ready').status_code == 200
