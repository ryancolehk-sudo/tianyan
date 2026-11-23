"""Rebuildable metadata index for immutable reference evidence."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = '0023_evidence_read_facets'
down_revision = '0022_data_resilience'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('evidence_read_facets',
        sa.Column('evidence_id', UUID(), primary_key=True),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('trade_dates', ARRAY(sa.Text()), nullable=False),
        sa.Column('observed_day', sa.Text()))
    op.create_index('ix_evidence_read_observed', 'evidence_read_facets', ['observed_day', 'evidence_id'])
    # Only a trigger over the immutable source row may maintain this index.
    # It carries no owner/permission decisions and never replaces raw evidence.
    op.execute('REVOKE ALL ON evidence_read_facets FROM PUBLIC')
    op.execute('REVOKE INSERT, UPDATE, DELETE ON evidence_read_facets FROM jlpm_app')
    op.execute('GRANT SELECT ON evidence_read_facets TO jlpm_app')
    op.execute("""
    CREATE FUNCTION reference_trade_dates(payload jsonb) RETURNS text[]
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
      SELECT ARRAY(SELECT DISTINCT jsonb_array_elements_text(
        jsonb_path_query_array(payload, '$.rows[*].trade_date')))
    $$;
    CREATE FUNCTION index_evidence_read_facets() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$ BEGIN
      INSERT INTO evidence_read_facets(evidence_id,content_hash,trade_dates,observed_day)
      VALUES(NEW.id,NEW.content_hash,reference_trade_dates(NEW.payload),NEW.payload->>'observed_day')
      ON CONFLICT(evidence_id) DO UPDATE SET content_hash=EXCLUDED.content_hash,
        trade_dates=EXCLUDED.trade_dates,observed_day=EXCLUDED.observed_day;
      RETURN NULL;
    END $$;
    CREATE TRIGGER evidence_read_facets_insert AFTER INSERT ON evidence_versions
      FOR EACH ROW WHEN (NEW.kind='reference') EXECUTE FUNCTION index_evidence_read_facets();
    INSERT INTO evidence_read_facets(evidence_id,content_hash,trade_dates,observed_day)
      SELECT id,content_hash,reference_trade_dates(payload),payload->>'observed_day'
      FROM evidence_versions WHERE kind='reference';
    """)


def downgrade():
    raise RuntimeError('Restore a compatible application; preserve immutable evidence.')
