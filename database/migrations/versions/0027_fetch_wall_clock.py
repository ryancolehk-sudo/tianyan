"""Keep captured wall clocks when the operating system steps backwards."""
from alembic import op

revision='0027_fetch_wall_clock'
down_revision='0026_read_focus'
branch_labels=None
depends_on=None


def upgrade():
    # No rows, source clocks, receipts or hashes are rewritten. A wall clock
    # cannot enforce elapsed-time ordering; elapsed_ms remains nonnegative.
    op.execute("SET LOCAL lock_timeout='8s'")
    op.drop_constraint('data_fetch_runs_check','data_fetch_runs',type_='check')
    op.execute("COMMENT ON COLUMN data_fetch_runs.started_at IS 'Request wall-clock timestamp; may exceed received_at after an OS clock backstep. Not an elapsed-time clock.'")
    op.execute("COMMENT ON COLUMN data_fetch_runs.received_at IS 'Actual response receipt wall clock, never clamped to the request start. Supplier market clocks remain separate.'")


def downgrade():
    raise RuntimeError('Use a compatible application; preserve actual captured clocks and immutable evidence.')
