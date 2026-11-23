"""Private frozen research runs, append-only eight-section reports and observations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '0017_tomorrow_research'
down_revision = '0016_read_cache'
branch_labels = None
depends_on = None


def ident(name='id', target=None, primary=False, nullable=False):
    args = [sa.ForeignKey(target)] if target else []
    return sa.Column(name, sa.Uuid(), *args, primary_key=primary, nullable=nullable)


def stamp(name='created_at', nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable,
                     server_default=None if nullable else sa.func.now())


def upgrade():
    op.create_table('tomorrow_settings', ident('owner_id', 'users.id', primary=True),
        sa.Column('revision', sa.Integer(), nullable=False), sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('use_ai', sa.Boolean(), nullable=False), sa.Column('profile', pg.JSONB(), nullable=False),
        stamp(), stamp('updated_at'))
    op.create_table('tomorrow_snapshots', ident(primary=True), ident('owner_id', 'users.id'),
        stamp('as_of_time'), sa.Column('manifest', pg.JSONB(), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False), ident('legacy_snapshot_id', 'snapshots.id', nullable=True), stamp())
    op.create_table('tomorrow_runs', ident(primary=True), ident('owner_id', 'users.id'),
        ident('job_id', 'jobs.id'), sa.Column('source_trade_day', sa.Date(), nullable=False),
        sa.Column('target_trade_day', sa.Date()), stamp('as_of_time'),
        sa.Column('slot', sa.String(20), nullable=False), sa.Column('state', sa.String(30), nullable=False),
        sa.Column('stage', sa.String(120), nullable=False), sa.Column('use_ai', sa.Boolean(), nullable=False),
        sa.Column('dedup_key', sa.String(64), nullable=False), sa.Column('input_hash', sa.String(64)),
        sa.Column('bundle', pg.JSONB()), sa.Column('stages', pg.JSONB(), nullable=False),
        sa.Column('config_snapshot', pg.JSONB(), nullable=False), sa.Column('limitations', pg.JSONB(), nullable=False),
        sa.Column('preparation', pg.JSONB(), nullable=False), ident('base_report_id', nullable=True),
        ident('snapshot_id', 'tomorrow_snapshots.id', nullable=True),
        sa.Column('error_code', sa.String(80)), stamp(), stamp('completed_at', True),
        sa.UniqueConstraint('owner_id', 'dedup_key'), sa.UniqueConstraint('job_id'))
    op.create_index('ix_tomorrow_runs_owner_day', 'tomorrow_runs', ['owner_id', 'source_trade_day', 'created_at'])
    op.create_table('tomorrow_reports', ident(primary=True), ident('owner_id', 'users.id'),
        ident('run_id', 'tomorrow_runs.id'), sa.Column('revision_number', sa.Integer(), nullable=False),
        ident('based_on_id', 'tomorrow_reports.id', nullable=True),
        sa.Column('author_type', sa.String(20), nullable=False), sa.Column('payload', pg.JSONB(), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False), stamp(),
        sa.UniqueConstraint('run_id', 'revision_number'), sa.UniqueConstraint('id', 'owner_id'),
        sa.CheckConstraint('revision_number > 0'))
    op.create_index('ix_tomorrow_reports_owner_time', 'tomorrow_reports', ['owner_id', 'created_at'])
    op.create_table('tomorrow_confirmations', ident(primary=True), ident('owner_id', 'users.id'),
        ident('report_id', 'tomorrow_reports.id'), stamp(), sa.UniqueConstraint('report_id'))
    op.create_table('tomorrow_validations', ident(primary=True), ident('owner_id', 'users.id'),
        ident('report_id', 'tomorrow_reports.id'), sa.Column('scenario_id', sa.String(200), nullable=False),
        sa.Column('status', sa.String(40), nullable=False), sa.Column('comment', sa.String(4000), nullable=False),
        sa.Column('evidence_refs', pg.JSONB(), nullable=False), sa.Column('observation', pg.JSONB(), nullable=False), stamp())
    op.create_index('ix_tomorrow_validations_report_time', 'tomorrow_validations', ['report_id', 'created_at'])
    # Even the application role cannot rewrite a published research fact or observation.
    op.execute('GRANT SELECT, INSERT, UPDATE ON tomorrow_settings, tomorrow_runs TO jlpm_app')
    op.execute('GRANT SELECT, INSERT ON tomorrow_snapshots, tomorrow_reports, tomorrow_confirmations, tomorrow_validations TO jlpm_app')
    op.execute('REVOKE UPDATE, DELETE ON tomorrow_snapshots, tomorrow_reports, tomorrow_confirmations, tomorrow_validations FROM jlpm_app')
    op.execute('REVOKE DELETE ON tomorrow_settings, tomorrow_runs FROM jlpm_app')


def downgrade():
    for table in ('tomorrow_validations', 'tomorrow_confirmations', 'tomorrow_reports', 'tomorrow_runs', 'tomorrow_snapshots', 'tomorrow_settings'):
        op.drop_table(table, if_exists=True)
