"""Service connections, manual AI jobs and per-request operational records."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '0014_data_center'
down_revision = '0013_news_center'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('service_connections',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('template', sa.String(100), nullable=False),
        sa.Column('endpoint', sa.String(2000), nullable=False),
        sa.Column('protocol', sa.String(40)),
        sa.Column('model', sa.String(200)),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('secret_ref', sa.String(200)),
        sa.Column('options', pg.JSONB(), nullable=False),
        sa.Column('source_ids', pg.JSONB(), nullable=False),
        sa.Column('last_test', pg.JSONB()),
        sa.Column('tested_revision', sa.Integer()),
        sa.Column('archived', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('model','market','news')"),
        sa.CheckConstraint('revision > 0'))
    op.create_table('ai_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('default_connection_id', sa.Uuid(), sa.ForeignKey('service_connections.id')),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.CheckConstraint('id = 1'), sa.CheckConstraint('revision > 0'))
    op.create_table('ai_bindings',
        sa.Column('feature', sa.String(60), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('connection_id', sa.Uuid(), sa.ForeignKey('service_connections.id')),
        sa.Column('fallback_connection_id', sa.Uuid(), sa.ForeignKey('service_connections.id')),
        sa.Column('revision', sa.Integer(), nullable=False))
    op.create_table('ai_runs',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('owner_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('job_id', sa.Uuid(), sa.ForeignKey('jobs.id'), nullable=False, unique=True),
        sa.Column('feature', sa.String(60), nullable=False),
        sa.Column('state', sa.String(30), nullable=False),
        sa.Column('stage', sa.String(120), nullable=False),
        sa.Column('connection_id', sa.Uuid(), sa.ForeignKey('service_connections.id'), nullable=False),
        sa.Column('model', sa.String(200), nullable=False),
        sa.Column('trade_day', sa.Date()),
        sa.Column('news_id', sa.Uuid(), sa.ForeignKey('evidence_versions.id')),
        sa.Column('as_of_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('config_snapshot', pg.JSONB(), nullable=False),
        sa.Column('input_hash', sa.String(64), nullable=False),
        sa.Column('dedup_key', sa.String(64), nullable=False, unique=True),
        sa.Column('input_data', pg.JSONB(), nullable=False),
        sa.Column('evidence_refs', pg.JSONB(), nullable=False),
        sa.Column('limitations', pg.JSONB(), nullable=False),
        sa.Column('result', pg.JSONB()),
        sa.Column('usage', pg.JSONB()),
        sa.Column('error_code', sa.String(80)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(timezone=True)))
    op.create_index('ix_ai_runs_owner_created', 'ai_runs', ['owner_id', 'created_at', 'id'])
    op.create_table('ai_attempts',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('run_id', sa.Uuid(), sa.ForeignKey('ai_runs.id')),
        sa.Column('owner_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('connection_id', sa.Uuid(), sa.ForeignKey('service_connections.id')),
        sa.Column('purpose', sa.String(20), nullable=False),
        sa.Column('service_name', sa.String(200), nullable=False),
        sa.Column('model', sa.String(200)),
        sa.Column('state', sa.String(30), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('sent_at', sa.DateTime(timezone=True)),
        sa.Column('completed_at', sa.DateTime(timezone=True)),
        sa.Column('elapsed_ms', sa.Integer()),
        sa.Column('error_code', sa.String(80)),
        sa.Column('input_tokens', sa.Integer()),
        sa.Column('output_tokens', sa.Integer()),
        sa.Column('cost_cny', sa.Numeric(20, 8)),
        sa.Column('retry_at', sa.DateTime(timezone=True)),
        sa.Column('request_count', sa.Integer(), nullable=False),
        sa.Column('safe_details', pg.JSONB(), nullable=False))
    op.create_index('ix_ai_attempts_time', 'ai_attempts', ['started_at', 'id'])
    op.create_index('ix_ai_attempts_run', 'ai_attempts', ['run_id'])
    op.create_table('ai_probe_proofs',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('owner_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('tested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('result', pg.JSONB(), nullable=False))
    op.create_index('ix_ai_probe_proofs_expires_at', 'ai_probe_proofs', ['expires_at'])
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON service_connections, ai_settings, ai_bindings, ai_runs, ai_attempts, ai_probe_proofs TO jlpm_app')


def downgrade():
    raise RuntimeError('Disable AI jobs and use the compatible previous application; retain connection and generation history.')
