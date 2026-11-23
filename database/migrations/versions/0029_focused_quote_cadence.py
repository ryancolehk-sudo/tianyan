"""Permit explicitly configured fast quote intervals without changing any row."""
from alembic import op

revision = '0029_focused_quote_cadence'
down_revision = '0028_selected_funds_invalidation'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE source_settings DROP CONSTRAINT source_settings_interval_seconds_check')
    op.execute('ALTER TABLE source_settings ADD CONSTRAINT source_settings_interval_seconds_check '
               'CHECK (interval_seconds BETWEEN 3 AND 86400)')


def downgrade():
    # Refuse a rollback until the explicit fast-source configuration has been
    # restored; never silently rewrite the user's cadence or scope.
    op.execute('ALTER TABLE source_settings DROP CONSTRAINT source_settings_interval_seconds_check')
    op.execute('ALTER TABLE source_settings ADD CONSTRAINT source_settings_interval_seconds_check '
               'CHECK (interval_seconds BETWEEN 15 AND 86400)')
