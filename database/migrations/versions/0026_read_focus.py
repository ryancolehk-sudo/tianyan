"""Prioritize visible live panels without starving prepared reads."""
from alembic import op

revision = '0026_read_focus'
down_revision = '0025_read_wakeup'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE read_cache_entries ADD COLUMN focused_at timestamptz;
    DROP TRIGGER read_cache_wakeup ON read_cache_entries;
    CREATE TRIGGER read_cache_wakeup AFTER INSERT OR UPDATE OF dirty,refresh_after,focused_at
      ON read_cache_entries FOR EACH ROW WHEN (NEW.dirty)
      EXECUTE FUNCTION wake_derived_read_worker();
    """)


def downgrade():
    raise RuntimeError('Restore a compatible application; preserve immutable evidence.')
