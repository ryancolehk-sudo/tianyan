"""Index member capture identity without decoding unrelated historical tables."""
from alembic import op
import sqlalchemy as sa

revision = '0030_ths_member_capture_facet'
down_revision = '0029_focused_quote_cadence'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('evidence_read_facets', sa.Column('ths_member_capture_id', sa.Text()))
    op.execute("""
    CREATE OR REPLACE FUNCTION index_evidence_read_facets() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$ BEGIN
      INSERT INTO evidence_read_facets(evidence_id,content_hash,trade_dates,observed_day,
        dataset,row_dataset,requested_day,batch_hash,ths_member_capture_id)
      VALUES(NEW.id,NEW.content_hash,reference_trade_dates(NEW.payload),NEW.payload->>'observed_day',
        NEW.payload->>'dataset',NEW.payload->'rows'->0->>'dataset',
        NEW.payload->'rows'->0->>'requested_day',NEW.payload->'rows'->0->>'batch_hash',
        CASE WHEN NEW.payload->>'dataset'='tushare.ths_member'
          THEN NEW.payload->'rows'->0->>'capture_id' ELSE NULL END)
      ON CONFLICT(evidence_id) DO UPDATE SET content_hash=EXCLUDED.content_hash,
        trade_dates=EXCLUDED.trade_dates,observed_day=EXCLUDED.observed_day,
        dataset=EXCLUDED.dataset,row_dataset=EXCLUDED.row_dataset,
        requested_day=EXCLUDED.requested_day,batch_hash=EXCLUDED.batch_hash,
        ths_member_capture_id=EXCLUDED.ths_member_capture_id;
      RETURN NULL;
    END $$;
    UPDATE evidence_read_facets f SET ths_member_capture_id=
      CASE WHEN e.payload->>'dataset'='tushare.ths_member'
        THEN e.payload->'rows'->0->>'capture_id' ELSE NULL END
      FROM evidence_versions e
      WHERE f.dataset='tushare.ths_member'
        AND e.id=f.evidence_id AND e.content_hash=f.content_hash;
    """)


def downgrade():
    raise RuntimeError('Keep this rebuildable index and use a schema-compatible application rollback.')
