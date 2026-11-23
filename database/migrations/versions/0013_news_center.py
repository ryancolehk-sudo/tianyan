"""News indexes, immutable editions and per-account reading states."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '0013_news_center'
down_revision = '0012_watchlist'
branch_labels = None
depends_on = None


def fk(name, target, *, primary=False):
    return sa.Column(name, sa.Uuid(), sa.ForeignKey(target), primary_key=primary, nullable=False)


def stamp(name, *, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade():
    # Frozen definitions must not import future application model changes.
    op.create_table('news_articles', sa.Column('id', sa.Uuid(), primary_key=True),
        fk('source_id', 'providers.id'), sa.Column('source_key', sa.String(200), nullable=False),
        fk('latest_evidence_id', 'evidence_versions.id'), stamp('first_received_at'), stamp('updated_at'),
        sa.Column('canonical_url', sa.String(2000), nullable=False), sa.Column('duplicate_key', sa.String(64), nullable=False),
        sa.Column('category', sa.String(30), nullable=False), sa.UniqueConstraint('source_id', 'source_key'))
    op.create_index('ix_news_article_receipt', 'news_articles', ['updated_at', 'id'])
    op.create_index('ix_news_article_duplicate', 'news_articles', ['duplicate_key'])
    op.create_table('news_user_states', fk('owner_id', 'users.id', primary=True), fk('article_id', 'news_articles.id', primary=True),
        sa.Column('read_hash', sa.String(64)), sa.Column('read_versions', JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('saved', sa.Boolean(), nullable=False),
        sa.Column('following', sa.Boolean(), nullable=False), stamp('updated_at'))
    op.create_table('news_center_state', sa.Column('id', sa.Integer(), primary_key=True), stamp('started_at'),
        sa.Column('first_day', sa.Date(), nullable=False), stamp('next_check_at'), sa.CheckConstraint('id = 1'))
    op.create_table('news_editions', sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('period', sa.String(8), nullable=False), sa.Column('day', sa.Date(), nullable=False),
        sa.Column('issue_number', sa.Integer(), nullable=False), stamp('window_start'), stamp('window_end'), stamp('generated_at'),
        sa.Column('recipe_version', sa.String(80), nullable=False), sa.Column('coverage_status', sa.String(12), nullable=False),
        sa.Column('coverage_note', sa.String(2000), nullable=False), sa.Column('manifest', JSONB(), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False), sa.UniqueConstraint('period', 'day'),
        sa.UniqueConstraint('period', 'issue_number'), sa.CheckConstraint("period IN ('day','week','month')"),
        sa.CheckConstraint('window_end > window_start'))
    op.create_index('ix_news_edition_date', 'news_editions', ['period', 'day'])
    op.create_table('news_edition_items', fk('edition_id', 'news_editions.id', primary=True),
        fk('article_id', 'news_articles.id', primary=True), fk('evidence_id', 'evidence_versions.id'),
        sa.Column('position', sa.Integer(), nullable=False), sa.Column('category', sa.String(30), nullable=False),
        sa.Column('late', sa.Boolean(), nullable=False), sa.UniqueConstraint('edition_id', 'position'),
        sa.UniqueConstraint('edition_id', 'article_id'))
    op.create_table('news_source_coverage', fk('source_id', 'providers.id', primary=True),
        fk('fetch_run_id', 'data_fetch_runs.id'), stamp('recorded_at'), sa.Column('coverage', JSONB(), nullable=False))
    op.create_table('news_ai_versions', sa.Column('id', sa.Uuid(), primary_key=True), fk('edition_id', 'news_editions.id'),
        stamp('created_at'), stamp('completed_at', nullable=True), sa.Column('state', sa.String(12), nullable=False),
        sa.Column('connection_id', sa.String(80), nullable=False), sa.Column('model', sa.String(200)),
        sa.Column('prompt_version', sa.String(80)), sa.Column('input_hash', sa.String(64), nullable=False),
        sa.Column('result', JSONB()), sa.Column('usage', JSONB()), sa.Column('error_code', sa.String(80)),
        sa.UniqueConstraint('edition_id'), sa.CheckConstraint("state IN ('running','complete','failed')"))
    for table in ('news_articles', 'news_user_states', 'news_center_state', 'news_editions',
                  'news_edition_items', 'news_source_coverage', 'news_ai_versions'):
        op.execute(f'GRANT SELECT, INSERT, UPDATE ON {table} TO jlpm_app')
    op.execute('REVOKE UPDATE, DELETE ON news_editions, news_edition_items FROM jlpm_app')
    op.create_index('ix_news_evidence_source_receipt', 'evidence_versions', ['source_id', 'received_at', 'id'],
                    postgresql_where=sa.text("kind = 'news' AND visibility = 'shared'"))


def downgrade():
    raise RuntimeError('Use an isolated backup restore; preserve saved news and published editions.')
