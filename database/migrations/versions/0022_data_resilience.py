"""Persistent automatic recovery decisions and fair projection refresh attempts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = '0022_data_resilience'
down_revision = '0021_recovery_coverage'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('read_cache_entries', sa.Column('last_attempt_at', sa.DateTime(timezone=True)))
    op.create_table('read_cache_dispatch',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('turn',sa.Integer(),nullable=False,server_default='0'),
        sa.CheckConstraint('id = 1'),sa.CheckConstraint('turn BETWEEN 0 AND 3'))
    op.execute('INSERT INTO read_cache_dispatch(id,turn) VALUES(1,0)')
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON read_cache_dispatch TO jlpm_app')
    op.create_table('data_health',
        sa.Column('id', UUID(), primary_key=True),
        sa.Column('owner_id', UUID(), nullable=False),
        sa.Column('primary_source_id', UUID()),
        sa.Column('active_source_id', UUID()),
        sa.Column('capability', sa.String(100), nullable=False),
        sa.Column('scope_key', sa.String(64), nullable=False),
        sa.Column('scope', JSONB(), nullable=False),
        sa.Column('trade_day', sa.Date(), nullable=False),
        sa.Column('state', sa.String(20), nullable=False),
        sa.Column('stage', sa.String(40), nullable=False),
        sa.Column('last_valid_at', sa.DateTime(timezone=True)),
        sa.Column('last_source_time', sa.DateTime(timezone=True)),
        sa.Column('reason', sa.String(100)),
        sa.Column('next_action', sa.String(40)),
        sa.Column('next_action_at', sa.DateTime(timezone=True)),
        sa.Column('missing_count', sa.Integer()),
        sa.Column('failure_count', sa.Integer(), nullable=False),
        sa.Column('recovery_successes', sa.Integer(), nullable=False),
        sa.Column('last_probe_time', sa.DateTime(timezone=True)),
        sa.Column('last_run_id', UUID()),
        sa.Column('configuration_sha256', sa.String(64), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('owner_id', 'primary_source_id', 'capability', 'scope_key', 'trade_day', name='uq_data_health_scope', postgresql_nulls_not_distinct=True),
        sa.CheckConstraint("state IN ('normal','fallback','recovering','attention')"))
    op.create_index('ix_data_health_owner_day', 'data_health', ['owner_id', 'trade_day'])
    op.create_index('ix_data_health_action', 'data_health', ['state', 'next_action_at'])
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON data_health TO jlpm_app')
    op.execute("""
    CREATE FUNCTION invalidate_data_health_route() RETURNS trigger
    LANGUAGE plpgsql AS $$ BEGIN
      IF TG_OP='INSERT' THEN
        IF NEW.active_source_id IS NULL THEN RETURN NULL; END IF;
      ELSIF TG_OP='DELETE' THEN
        IF OLD.active_source_id IS NULL THEN RETURN NULL; END IF;
      ELSIF OLD.active_source_id IS NOT DISTINCT FROM NEW.active_source_id THEN
        RETURN NULL;
      END IF;
      PERFORM bump_read_dependency('routing-health');
      RETURN NULL;
    END $$;
    CREATE TRIGGER data_health_route_changed
      AFTER INSERT OR UPDATE OF active_source_id OR DELETE ON data_health
      FOR EACH ROW EXECUTE FUNCTION invalidate_data_health_route();
    """)
    # Earlier migrations let PostgreSQL assign the check name. Discover that
    # name instead of assuming a particular generated naming convention.
    checks = sa.inspect(op.get_bind()).get_check_constraints('jobs')
    for check in checks:
        if 'attempts' in check['sqltext']:
            op.drop_constraint(check['name'], 'jobs', type_='check')
    op.create_check_constraint('ck_job_attempts', 'jobs', 'attempts BETWEEN 0 AND 5')


def downgrade():
    raise RuntimeError('Preserve automatic recovery decisions; restore a compatible application.')
