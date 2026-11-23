"""Complete small reference facets without repeatedly decoding original tables."""
from alembic import op
import sqlalchemy as sa

revision = '0024_read_metadata'
down_revision = '0023_evidence_read_facets'
branch_labels = None
depends_on = None


def upgrade():
    for column in ('dataset', 'row_dataset', 'requested_day', 'batch_hash'):
        op.add_column('evidence_read_facets', sa.Column(column, sa.Text()))
    op.execute("""
    CREATE OR REPLACE FUNCTION index_evidence_read_facets() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$ BEGIN
      INSERT INTO evidence_read_facets(evidence_id,content_hash,trade_dates,observed_day,
        dataset,row_dataset,requested_day,batch_hash)
      VALUES(NEW.id,NEW.content_hash,reference_trade_dates(NEW.payload),NEW.payload->>'observed_day',
        NEW.payload->>'dataset',NEW.payload->'rows'->0->>'dataset',
        NEW.payload->'rows'->0->>'requested_day',NEW.payload->'rows'->0->>'batch_hash')
      ON CONFLICT(evidence_id) DO UPDATE SET content_hash=EXCLUDED.content_hash,
        trade_dates=EXCLUDED.trade_dates,observed_day=EXCLUDED.observed_day,
        dataset=EXCLUDED.dataset,row_dataset=EXCLUDED.row_dataset,
        requested_day=EXCLUDED.requested_day,batch_hash=EXCLUDED.batch_hash;
      RETURN NULL;
    END $$;
    UPDATE evidence_read_facets f SET dataset=e.payload->>'dataset',
      row_dataset=e.payload->'rows'->0->>'dataset',
      requested_day=e.payload->'rows'->0->>'requested_day',
      batch_hash=e.payload->'rows'->0->>'batch_hash'
      FROM evidence_versions e WHERE e.id=f.evidence_id AND e.content_hash=f.content_hash;
    """)


def downgrade():
    raise RuntimeError('Restore a compatible application; preserve immutable evidence.')
