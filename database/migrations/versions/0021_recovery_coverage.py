"""Durable, owner-scoped collection coverage and missing intervals."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = '0021_recovery_coverage'
down_revision = '0020_data_routes'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('worker_heartbeats', sa.Column('progress_at', sa.DateTime(timezone=True)))
    op.add_column('worker_heartbeats', sa.Column('job_id', UUID()))
    op.create_table('recovery_coverage',
        sa.Column('id', UUID(), primary_key=True),
        sa.Column('owner_id', UUID(), nullable=False),
        sa.Column('source_id', UUID(), nullable=False),
        sa.Column('dataset', sa.String(100), nullable=False),
        sa.Column('scope_key', sa.String(64), nullable=False),
        sa.Column('scope', JSONB(), nullable=False),
        sa.Column('trade_day', sa.Date(), nullable=False),
        sa.Column('interval', sa.String(20), nullable=False),
        sa.Column('state', sa.String(20), nullable=False),
        sa.Column('expected_count', sa.Integer()), sa.Column('actual_count', sa.Integer()),
        sa.Column('missing_ranges', JSONB(), nullable=False),
        sa.Column('job_id', UUID()), sa.Column('reason', sa.String(100)),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('next_retry_at', sa.DateTime(timezone=True)),
        sa.Column('configuration_sha256', sa.String(64), nullable=False),
        sa.UniqueConstraint('owner_id', 'source_id', 'dataset', 'scope_key', 'trade_day', 'interval', name='uq_recovery_scope'),
        sa.CheckConstraint("state IN ('missing','partial','complete','queued','running','waiting','blocked','unsupported')"))
    op.create_index('ix_recovery_due', 'recovery_coverage', ['state', 'next_retry_at'])
    op.create_index('ix_recovery_owner_day', 'recovery_coverage', ['owner_id', 'trade_day'])
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON recovery_coverage TO jlpm_app')


def downgrade():
    raise RuntimeError('Preserve recovery progress; use a schema-compatible application.')
