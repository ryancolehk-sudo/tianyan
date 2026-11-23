"""Personal settings and isolated local maintenance queue."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '0017_system_settings'
down_revision = '0016_read_cache'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('user_preferences',
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('start_page', sa.String(20), nullable=False, server_default='workbench'),
        sa.Column('review_lookback', sa.Integer(), nullable=False, server_default='20'),
        sa.Column('news_category', sa.String(20), nullable=False, server_default='精选'),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("start_page IN ('workbench','review','news')"),
        sa.CheckConstraint('review_lookback IN (20,60)'),
        sa.CheckConstraint("news_category IN ('精选','全部')"), sa.CheckConstraint('revision>=1'))
    op.create_table('maintenance_jobs',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('owner_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('kind', sa.String(30), nullable=False),
        sa.Column('status', sa.String(30), nullable=False, server_default='queued'),
        sa.Column('stage', sa.String(80), nullable=False, server_default='queued'),
        sa.Column('message', sa.String(500), nullable=False, server_default='等待维护进程处理。'),
        sa.Column('error_code', sa.String(80)),
        sa.Column('params', pg.JSONB(), nullable=False, server_default='{}'),
        sa.Column('result', pg.JSONB()), sa.Column('idempotency_key', sa.String(128)),
        sa.Column('lease_token', sa.Uuid()), sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('started_at', sa.DateTime(timezone=True)), sa.Column('completed_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('backup','update.download','update.install')"),
        sa.CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled','manual_recovery')"))
    op.create_index('ix_maintenance_jobs_owner_id', 'maintenance_jobs', ['owner_id'])
    op.create_index('ix_maintenance_claim', 'maintenance_jobs', ['status', 'created_at', 'lease_until'])
    op.execute("CREATE UNIQUE INDEX uq_maintenance_active_exclusive ON maintenance_jobs ((1)) "
               "WHERE kind IN ('backup','update.install') AND status IN ('queued','running')")
    op.create_table('backup_records',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('owner_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('job_id', sa.Uuid(), sa.ForeignKey('maintenance_jobs.id'), nullable=False, unique=True),
        sa.Column('name', sa.String(200), nullable=False), sa.Column('location_key', sa.String(240), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(timezone=True)),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False, server_default='0'), sa.Column('sha256', sa.String(64)),
        sa.Column('integrity_state', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('recovery_state', sa.String(20), nullable=False, server_default='unverified'),
        sa.Column('recovery_verified_at', sa.DateTime(timezone=True)),
        sa.Column('app_mode', sa.String(10), nullable=False), sa.Column('schema_revision', sa.String(80), nullable=False),
        sa.Column('build_revision', sa.String(80), nullable=False),
        sa.CheckConstraint("integrity_state IN ('pending','passed','failed')"),
        sa.CheckConstraint("recovery_state IN ('unverified','passed','failed')"), sa.CheckConstraint('size_bytes>=0'))
    op.create_index('ix_backup_records_owner_id', 'backup_records', ['owner_id'])
    op.create_table('system_settings_state',
        sa.Column('id', sa.Integer(), primary_key=True), sa.CheckConstraint('id=1'),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('upgrade_job_id', sa.Uuid()), sa.Column('stage', sa.String(80), nullable=False, server_default='idle'),
        sa.Column('started_at', sa.DateTime(timezone=True)),
        sa.Column('updater_heartbeat_at', sa.DateTime(timezone=True)), sa.Column('backup_heartbeat_at', sa.DateTime(timezone=True)))
    op.create_table('system_update_state',
        sa.Column('id', sa.Integer(), primary_key=True), sa.CheckConstraint('id=1'),
        sa.Column('checked_at', sa.DateTime(timezone=True)),
        sa.Column('status', sa.String(30), nullable=False, server_default='unchecked'),
        sa.Column('message', sa.String(500), nullable=False, server_default='尚未检查更新。'),
        sa.Column('error_code', sa.String(80)), sa.Column('manifest', pg.JSONB()), sa.Column('signature', sa.Text()),
        sa.Column('downloaded_at', sa.DateTime(timezone=True)), sa.Column('package_name', sa.String(240)),
        sa.Column('release_id', sa.String(160)))
    op.execute('INSERT INTO system_settings_state(id) VALUES(1)')
    op.execute('INSERT INTO system_update_state(id) VALUES(1)')
    op.add_column('audit_events', sa.Column('outcome', sa.String(20), nullable=True))
    op.add_column('audit_events', sa.Column('context', pg.JSONB(), nullable=True))
    op.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON user_preferences,maintenance_jobs,backup_records,system_settings_state,system_update_state TO jlpm_app')
    # Existing installations receive the least-privileged maintenance role
    # separately. Fresh installs already create it during PostgreSQL init.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='jlpm_maintenance') THEN
        GRANT INSERT,UPDATE,DELETE ON maintenance_jobs,backup_records,system_settings_state,system_update_state TO jlpm_maintenance;
        GRANT INSERT,UPDATE ON audit_events TO jlpm_maintenance;
      END IF;
    END $$""")


def downgrade():
    raise RuntimeError('Keep system settings and maintenance history; use a schema-compatible application rollback.')
