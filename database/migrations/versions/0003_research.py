"""Add immutable research revisions and append-only publication events."""
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0003_research"
down_revision = "0002_evidence"
branch_labels = None
depends_on = None


def upgrade():
    ddl = (Path(__file__).parents[1] / "sql/0003_research.sql").read_text(encoding="utf-8")
    for statement in ddl.split("-- statement --"):
        if statement.strip():
            op.execute(sa.text(statement))
    op.execute("""CREATE FUNCTION reject_research_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'immutable research resource' USING ERRCODE='23514'; END $$""")
    for table in ["document_revisions", "revision_links", "publication_events", "plan_scenarios"]:
        op.execute(f"CREATE TRIGGER immutable_research BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_research_mutation()")
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM jlpm_app")
    op.execute("""CREATE FUNCTION protect_research_document() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'research cannot be deleted' USING ERRCODE='23514'; END IF;
        IF NEW.head_revision_number <> OLD.head_revision_number + 1 OR
          (to_jsonb(NEW)-'head_revision_number') IS DISTINCT FROM (to_jsonb(OLD)-'head_revision_number') THEN
          RAISE EXCEPTION 'research identity is immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("CREATE TRIGGER research_head_only BEFORE UPDATE OR DELETE ON research_documents FOR EACH ROW EXECUTE FUNCTION protect_research_document()")
    op.execute("""CREATE FUNCTION protect_revision_insert() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE d research_documents;
      BEGIN
        SELECT * INTO STRICT d FROM research_documents WHERE id=NEW.document_id AND owner_id=NEW.owner_id FOR UPDATE;
        IF NEW.author_id<>NEW.owner_id OR (NEW.content->>'type') IS DISTINCT FROM d.kind THEN
          RAISE EXCEPTION 'revision identity mismatch' USING ERRCODE='23514';
        END IF;
        IF NEW.revision_number=1 THEN
          IF NEW.based_on_revision_id IS NOT NULL OR d.head_revision_number<>1 THEN
            RAISE EXCEPTION 'invalid initial revision' USING ERRCODE='23514';
          END IF;
        ELSE
          PERFORM 1 FROM document_revisions WHERE id=NEW.based_on_revision_id AND document_id=d.id
            AND owner_id=NEW.owner_id AND revision_number=NEW.revision_number-1;
          IF NOT FOUND OR NEW.revision_number<>d.head_revision_number+1 THEN
            RAISE EXCEPTION 'invalid revision predecessor' USING ERRCODE='23514';
          END IF;
        END IF;
        PERFORM 1 FROM snapshots WHERE id=NEW.snapshot_id AND owner_id=NEW.owner_id AND state='ready' AND recorded_at<=NEW.recorded_at;
        IF NOT FOUND THEN RAISE EXCEPTION 'research snapshot unavailable' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$""")
    op.execute("CREATE TRIGGER research_revision_scope BEFORE INSERT ON document_revisions FOR EACH ROW EXECUTE FUNCTION protect_revision_insert()")
    op.execute("""CREATE FUNCTION protect_revision_link() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        PERFORM 1 FROM document_revisions c JOIN research_documents cd ON cd.id=c.document_id
          JOIN document_revisions p ON p.id=NEW.parent_revision_id JOIN research_documents pd ON pd.id=p.document_id
          WHERE c.id=NEW.child_revision_id AND c.owner_id=NEW.owner_id AND p.owner_id=NEW.owner_id
            AND cd.id<>pd.id AND cd.source_trade_day=pd.source_trade_day AND p.recorded_at<=c.recorded_at
            AND ((cd.kind='direction' AND pd.kind='review' AND NEW.relation='review_direction')
              OR (cd.kind='plan' AND pd.kind='direction' AND NEW.relation='direction_plan'))
            AND EXISTS(SELECT 1 FROM publication_events e WHERE e.revision_id=p.id AND e.kind='published' AND e.recorded_at<=c.recorded_at)
            AND NOT EXISTS(SELECT 1 FROM publication_events e WHERE e.revision_id=p.id AND e.kind='withdrawn');
        IF NOT FOUND THEN RAISE EXCEPTION 'invalid fixed research parent' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$""")
    op.execute("CREATE TRIGGER research_parent_scope BEFORE INSERT ON revision_links FOR EACH ROW EXECUTE FUNCTION protect_revision_link()")
    op.execute("""CREATE FUNCTION protect_publication_insert() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        PERFORM 1 FROM document_revisions r JOIN research_documents d ON d.id=r.document_id
          WHERE r.id=NEW.revision_id AND r.owner_id=NEW.owner_id AND r.recorded_at<=NEW.recorded_at
            AND (NEW.kind='withdrawn' OR r.revision_number=d.head_revision_number);
        IF NOT FOUND THEN RAISE EXCEPTION 'invalid publication revision' USING ERRCODE='23514'; END IF;
        IF NEW.kind='withdrawn' THEN
          PERFORM 1 FROM publication_events WHERE revision_id=NEW.revision_id AND kind='published' AND recorded_at<=NEW.recorded_at;
          IF NOT FOUND THEN RAISE EXCEPTION 'unpublished revision cannot be withdrawn' USING ERRCODE='23514'; END IF;
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("CREATE TRIGGER research_publication_scope BEFORE INSERT ON publication_events FOR EACH ROW EXECUTE FUNCTION protect_publication_insert()")
    op.execute("""CREATE FUNCTION protect_scenario_insert() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        PERFORM 1 FROM document_revisions r JOIN research_documents d ON d.id=r.document_id
          WHERE r.id=NEW.plan_revision_id AND r.owner_id=NEW.owner_id AND d.kind='plan'
            AND NEW.position>=0 AND r.content->'scenarios'->NEW.position=NEW.payload;
        IF NOT FOUND THEN RAISE EXCEPTION 'scenario revision mismatch' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$""")
    op.execute("CREATE TRIGGER research_scenario_scope BEFORE INSERT ON plan_scenarios FOR EACH ROW EXECUTE FUNCTION protect_scenario_insert()")


def downgrade():
    raise RuntimeError("Restore a private backup into a separate instance; never delete research revisions.")
