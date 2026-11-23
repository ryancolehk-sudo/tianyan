"""Account-scoped ordered watchlists; no market evidence is rewritten."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = '0012_watchlist'
down_revision = '0011_fetch_retry_deadline'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('watchlists',
        sa.Column('owner_id', sa.Uuid(), sa.ForeignKey('users.id'), primary_key=True),
        sa.Column('instrument_ids', postgresql.JSONB(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("jsonb_typeof(instrument_ids) = 'array' AND jsonb_array_length(instrument_ids) <= 100",
                           name='ck_watchlist_size'))
    op.execute('GRANT SELECT, INSERT, UPDATE ON watchlists TO jlpm_app')
    op.create_index('ix_reference_source_receipt', 'evidence_versions', ['source_id','received_at','id'],
                    postgresql_where=sa.text("kind = 'reference' AND visibility = 'shared'"))


def downgrade():
    raise RuntimeError('Restore a backup into a separate empty instance; retain account watchlists.')
