"""Only subscribed owners' current funds views depend on selected-object data."""
from alembic import op

revision = '0028_selected_funds_invalidation'
down_revision = '0027_fetch_wall_clock'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE OR REPLACE FUNCTION invalidate_derived_reads() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE item jsonb; family text; provider_kind text; requester text;
    BEGIN
      IF TG_OP='DELETE' THEN item=to_jsonb(OLD); ELSE item=to_jsonb(NEW); END IF;
      IF TG_TABLE_NAME='providers' THEN
        IF TG_OP='UPDATE' AND to_jsonb(OLD)=to_jsonb(NEW) THEN RETURN NULL; END IF;
        PERFORM bump_read_dependency('authorization');
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
          PERFORM bump_read_dependency('authorization');
        END IF;
        IF TG_TABLE_NAME='data_fetch_runs' THEN
          PERFORM bump_read_dependency('run:'||coalesce(family,'')); RETURN NULL;
        END IF;
        IF TG_TABLE_NAME='source_settings' THEN
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
          IF family='eastmoney_funds' AND item->>'kind'='reference'
            AND item->'payload'->>'dataset'='market.eastmoney.funds.selected_detail' THEN
            -- Public observations can serve several owners of this object.
            -- Each owner keeps their own demand/state and authorization scope.
            -- A selected detail must not dirty every stock and all four pages.
            FOR requester IN
              SELECT DISTINCT j.payload->'funds_focus_context'->>'requester_id'
              FROM jobs j
              WHERE j.kind='funds.focus'
                AND j.payload->>'source_id'=item->>'source_id'
                AND j.payload->'funds_focus_context'->>'day'=item->'payload'->>'observed_day'
                AND j.payload->'funds_focus_context'->>'category'=item->'payload'->'rows'->0->>'category'
                AND j.payload->'funds_focus_context'->>'code'=item->'payload'->'rows'->0->>'code'
                AND (j.payload->'funds_focus_context'->>'exchange') IS NOT DISTINCT FROM
                    (item->'payload'->'rows'->0->>'exchange')
                AND j.payload->'funds_focus_context'->>'requester_id' IS NOT NULL
            LOOP
              PERFORM bump_read_dependency('funds-selection:'||requester);
            END LOOP;
            RETURN NULL;
          END IF;
        END IF;
        IF family IS NOT NULL THEN PERFORM bump_read_dependency('family:'||family); END IF;
        IF provider_kind='news' THEN PERFORM bump_read_dependency('news'); END IF;
      ELSIF TG_TABLE_NAME LIKE 'news_%' OR TG_TABLE_NAME IN ('service_connections','ai_settings') THEN
        PERFORM bump_read_dependency('news');
      ELSE
        PERFORM bump_read_dependency('catalog');
      END IF;
      RETURN NULL;
    END $$;
    """)


def downgrade():
    raise RuntimeError('Use a compatible application; preserve immutable evidence and owner state.')
