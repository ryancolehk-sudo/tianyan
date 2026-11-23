"""Wake the local read worker after a dirty cache transaction commits."""
from alembic import op

revision = '0025_read_wakeup'
down_revision = '0024_read_metadata'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE FUNCTION wake_derived_read_worker() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      -- Identical notifications in a transaction coalesce; delivery happens
      -- only after commit. No owner, market values or credentials are sent.
      PERFORM pg_notify('tianyan_read_dirty','');
      RETURN NULL;
    END $$;
    CREATE TRIGGER read_cache_wakeup AFTER INSERT OR UPDATE OF dirty,refresh_after
      ON read_cache_entries FOR EACH ROW WHEN (NEW.dirty)
      EXECUTE FUNCTION wake_derived_read_worker();
    """)


def downgrade():
    raise RuntimeError('Restore a compatible application; preserve immutable evidence.')
