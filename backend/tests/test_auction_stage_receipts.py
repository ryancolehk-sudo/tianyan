"""An identical rolling payload needs a fresh post-checkpoint receipt."""
from datetime import UTC, datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market.auction_source import parse_auction
from app.market.collect import store_reference_batch
from app.market.models import SourceSettings
from app.market.service import source_id
from app.models import Provider
from test_auction_tables import CURRENT, current_body, configure_family
from test_data_sources import login


def test_each_live_receipt_is_archived_even_when_the_table_is_unchanged(client):
    headers = login(client); configure_family(client, headers, CURRENT)
    ident = source_id(CURRENT)
    def received(hour, minute, second=0): return datetime(2026, 10, 21, hour, minute, second, tzinfo=UTC)
    batch = parse_auction(current_body(), received(1, 21))
    with Session(engine()) as db, db.begin():
        provider, config = db.get(Provider, ident), db.get(SourceSettings, ident)
        assert store_reference_batch(db, provider, config, batch, received(1, 21)) == (2, 0)
        original = [(row.id, row.content_hash, row.received_at, row.payload) for row in db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == ident))]
        assert store_reference_batch(db, provider, config, batch, received(1, 24, 59)) == (2, 0)
        assert store_reference_batch(db, provider, config, batch, received(1, 25)) == (2, 0)
        assert store_reference_batch(db, provider, config, batch, received(1, 25)) == (0, 2)
        assert store_reference_batch(db, provider, config, batch, received(1, 26)) == (2, 0)
        # Replaying an earlier receipt cannot erase or replace the later one.
        assert store_reference_batch(db, provider, config, batch, received(1, 21)) == (0, 2)
        rows = list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == ident)))
        assert len(rows) == 8
        for ident, digest, clock, payload in original:
            row = db.get(EvidenceVersion, ident)
            assert (row.content_hash, row.received_at, row.payload) == (digest, clock, payload)
        assert sorted(row.version_number for row in rows) == [1, 1, 2, 2, 3, 3, 4, 4]
        assert sum(row.received_at == received(1, 25) for row in rows) == 2
