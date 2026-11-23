"""Durable derived reads with transactional dependency invalidation."""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '0016_read_cache'
down_revision = '0015_chatgpt_accounts'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('instance_state', sa.Column('cache_id', sa.Uuid(), nullable=True))
    op.execute(sa.text('UPDATE instance_state SET cache_id=:cache_id').bindparams(cache_id=uuid.uuid4()))
    op.alter_column('instance_state', 'cache_id', nullable=False)
    op.create_table('read_cache_dependencies',
        sa.Column('key', sa.String(160), primary_key=True),
        sa.Column('revision', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('last_txid', sa.BigInteger(), nullable=False, server_default='0'))
    op.create_table('read_cache_entries',
        sa.Column('key', sa.String(64), primary_key=True),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('scope', sa.String(64), nullable=False),
        sa.Column('operation', sa.String(50), nullable=False),
        sa.Column('params', pg.JSONB(), nullable=False),
        sa.Column('dependency_keys', pg.JSONB(), nullable=False),
        sa.Column('revision', sa.String(64)),
        sa.Column('payload', pg.JSONB()),
        sa.Column('built_at', sa.DateTime(timezone=True)),
        sa.Column('validated_at', sa.DateTime(timezone=True)),
        sa.Column('refresh_after', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('accessed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('dirty', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('lease_token', sa.Uuid()),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('last_error', sa.String(50)))
    op.create_index('ix_read_cache_entries_owner_id', 'read_cache_entries', ['owner_id'])
    op.create_index('ix_read_cache_queue', 'read_cache_entries', ['dirty', 'refresh_after'])
    op.create_index('ix_read_cache_retention', 'read_cache_entries', ['accessed_at'])
    op.create_index('ix_read_cache_dependency_keys', 'read_cache_entries', ['dependency_keys'], postgresql_using='gin')
    # PostgreSQL triggers cover ORM, bulk inserts and collector SQL alike. A
    # rolled-back ingestion cannot invalidate or publish a derived read.
    op.execute("""
    CREATE FUNCTION bump_read_dependency(dependency text) RETURNS void
    LANGUAGE plpgsql AS $$ DECLARE changed text; BEGIN
      -- A 6,000-stock ingestion changes a dependency once in its transaction,
      -- rather than rewriting all cached projections for every evidence row.
      INSERT INTO read_cache_dependencies(key,revision,last_txid)
      VALUES(dependency,1,txid_current())
      ON CONFLICT(key) DO UPDATE SET revision=read_cache_dependencies.revision+1,
        last_txid=txid_current()
      WHERE read_cache_dependencies.last_txid<>txid_current()
      RETURNING key INTO changed;
      IF changed IS NOT NULL THEN
        UPDATE read_cache_entries SET dirty=true,refresh_after=now()
        WHERE dependency_keys ? dependency;
      END IF;
    END $$;
    CREATE FUNCTION invalidate_derived_reads() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE item jsonb; family text; provider_kind text;
    BEGIN
      IF TG_OP='DELETE' THEN item=to_jsonb(OLD); ELSE item=to_jsonb(NEW); END IF;
      IF TG_TABLE_NAME='providers' THEN
        IF TG_OP='UPDATE' AND to_jsonb(OLD)=to_jsonb(NEW) THEN RETURN NULL; END IF;
        PERFORM bump_read_dependency('authorization');
        -- Authorization participates in every result and in browser scope.
      ELSIF TG_TABLE_NAME='users' THEN
        IF TG_OP='UPDATE' AND OLD.role=NEW.role AND OLD.enabled=NEW.enabled THEN RETURN NULL; END IF;
        PERFORM bump_read_dependency('user:'||(item->>'id'));
      ELSIF TG_TABLE_NAME='watchlists' THEN
        PERFORM bump_read_dependency('watch:'||(item->>'owner_id'));
      ELSIF TG_TABLE_NAME='news_user_states' THEN
        PERFORM bump_read_dependency('news-user:'||(item->>'owner_id'));
      ELSIF TG_TABLE_NAME IN ('evidence_versions','source_versions','data_fetch_runs','source_settings') THEN
        SELECT p.family,p.kind INTO family,provider_kind FROM providers p WHERE p.id=(item->>'source_id')::uuid;
        IF TG_TABLE_NAME='source_versions' THEN
          -- A descriptor correction can revoke the permission available to an
          -- explicit historical cutoff even if today's provider is unchanged.
          PERFORM bump_read_dependency('authorization');
        END IF;
        IF TG_TABLE_NAME='data_fetch_runs' THEN
          -- Runs affect capability/source status, never the saved stock data.
          PERFORM bump_read_dependency('run:'||coalesce(family,'')); RETURN NULL;
        END IF;
        IF TG_TABLE_NAME='source_settings' THEN
          -- Scheduling timestamps do not change the projected evidence.
          IF TG_OP='UPDATE' AND OLD.paused_reason IS NOT DISTINCT FROM NEW.paused_reason
            THEN RETURN NULL; END IF;
          IF provider_kind='news' THEN PERFORM bump_read_dependency('news'); END IF;
          RETURN NULL;
        END IF;
        IF TG_TABLE_NAME='evidence_versions' THEN
          IF item->>'visibility'='private' THEN RETURN NULL; END IF;
          IF item->>'kind'='news' THEN
            PERFORM bump_read_dependency('news'); RETURN NULL;
          END IF;
        END IF;
        IF family IS NOT NULL THEN
          PERFORM bump_read_dependency('family:'||family);
        END IF;
        IF provider_kind='news' THEN PERFORM bump_read_dependency('news'); END IF;
      ELSIF TG_TABLE_NAME LIKE 'news_%' OR TG_TABLE_NAME IN ('service_connections','ai_settings') THEN
        PERFORM bump_read_dependency('news');
      ELSE
        PERFORM bump_read_dependency('catalog');
      END IF;
      RETURN NULL;
    END $$;
    """)
    for table in ('providers', 'users', 'evidence_versions', 'source_versions',
                  'data_fetch_runs', 'source_settings', 'instruments', 'instrument_origins',
                  'calendar_versions', 'trading_sessions', 'watchlists', 'news_user_states',
                  'news_articles', 'news_editions', 'news_edition_items', 'news_source_coverage',
                  'news_ai_versions', 'news_center_state', 'service_connections', 'ai_settings'):
        op.execute(f'CREATE TRIGGER derived_read_changed AFTER INSERT OR UPDATE OR DELETE ON {table} '
                   'FOR EACH ROW EXECUTE FUNCTION invalidate_derived_reads()')
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON read_cache_dependencies,read_cache_entries TO jlpm_app')


def downgrade():
    # The previous application can ignore these disposable tables; do not
    # remove a stable browser-cache identity while reverting a release.
    raise RuntimeError('Disable read cache and use a schema-compatible application; keep source records unchanged.')
