"""Persist controlled retry deadlines without rewriting prior fetch history."""
import sqlalchemy as sa
from alembic import op

revision = '0011_fetch_retry_deadline'
down_revision = '0010_reference_datasets'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('data_fetch_runs', sa.Column('retry_not_before', sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint('ck_fetch_retry_deadline', 'data_fetch_runs',
        "retry_not_before IS NULL OR (retry_not_before >= received_at AND retry_not_before <= received_at + interval '1 day')")
    op.create_index('ix_fetch_retry_deadline', 'data_fetch_runs', ['source_id', 'retry_not_before'],
        postgresql_where=sa.text('retry_not_before IS NOT NULL'))
    # Freeze only the old operational schedule once. Reading next_run_at later
    # would misclassify normal scheduling as a new cooldown and never resume.
    op.add_column('source_settings', sa.Column('legacy_cooldown_until', sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("""UPDATE source_settings s SET legacy_cooldown_until=s.next_run_at
        FROM providers p WHERE p.id=s.source_id
        AND p.family IN ('ashare_public','ashare_breadth','ashare_sector_heat')
        AND s.next_run_at>clock_timestamp()
        AND (SELECT r.reason FROM data_fetch_runs r WHERE r.source_id=s.source_id
            ORDER BY r.received_at DESC,r.id DESC LIMIT 1)='rate_limited'"""))


def downgrade():
    raise RuntimeError('Restore a backup into a separate empty instance; retain recorded retry deadlines.')
