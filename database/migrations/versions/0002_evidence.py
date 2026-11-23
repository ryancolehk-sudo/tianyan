"""Add private evidence, stable snapshots and explicitly imported calendar versions."""
import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0002_evidence"
down_revision = "0001_p1"
branch_labels = None
depends_on = None


def upgrade():
    # Frozen DDL is independent of future ORM changes.
    ddl = (Path(__file__).parents[1] / "sql/0002_evidence.sql").read_text(encoding="utf-8")
    for statement in ddl.split("-- statement --"):
        if statement.strip():
            op.execute(sa.text(statement))
    op.add_column("audit_events", sa.Column("resource_type", sa.String(80)))
    op.add_column("audit_events", sa.Column("resource_id", sa.Uuid()))
    op.add_column("audit_events", sa.Column("changed_field_names", sa.dialects.postgresql.JSONB()))
    op.execute("""CREATE FUNCTION reject_p2_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'immutable evidence resource' USING ERRCODE='23514'; END $$""")
    for table in ["source_versions", "evidence_versions", "source_health", "calendar_versions", "trading_sessions", "snapshot_items"]:
        op.execute(f"CREATE TRIGGER immutable_resource BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_p2_mutation()")
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM jlpm_app")
    op.execute("""CREATE FUNCTION protect_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'snapshot cannot be deleted' USING ERRCODE='23514'; END IF;
        IF OLD.state <> 'pending' OR
          (to_jsonb(NEW)-'state'-'manifest'-'manifest_hash'-'error_code') IS DISTINCT FROM
          (to_jsonb(OLD)-'state'-'manifest'-'manifest_hash'-'error_code') THEN
          RAISE EXCEPTION 'snapshot cannot be changed' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END $$""")
    op.execute("CREATE TRIGGER freeze_snapshot BEFORE UPDATE OR DELETE ON snapshots FOR EACH ROW EXECUTE FUNCTION protect_snapshot()")
    op.execute("""CREATE FUNCTION protect_snapshot_item_insert() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        PERFORM 1 FROM snapshots WHERE id=NEW.snapshot_id AND state='pending' FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'snapshot is frozen' USING ERRCODE='23514'; END IF;
        PERFORM 1 FROM evidence_versions e JOIN snapshots s ON s.id=NEW.snapshot_id JOIN providers p ON p.id=e.source_id
          WHERE e.id=NEW.evidence_version_id AND e.content_hash=NEW.content_hash
            AND e.historical_eligible AND e.received_at<=s.as_of_time AND e.available_at<=s.as_of_time
            AND (e.event_at IS NULL OR e.event_at<=s.as_of_time)
            AND p.enabled AND p.configured AND p.entitlement_status='verified'
            AND p.rights->>'display'='allowed' AND p.rights->>'cache'='allowed'
            AND ((e.visibility='private' AND e.owner_id=s.owner_id) OR e.visibility='shared');
        IF NOT FOUND THEN RAISE EXCEPTION 'snapshot evidence scope mismatch' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END $$""")
    op.execute("CREATE TRIGGER snapshot_item_scope BEFORE INSERT ON snapshot_items FOR EACH ROW EXECUTE FUNCTION protect_snapshot_item_insert()")
    op.execute("""INSERT INTO providers(id,name,kind,adapter_version,configured,enabled,entitlement_status,capabilities,rights)
      VALUES ('""" + str(uuid.uuid5(uuid.NAMESPACE_URL, "jiulong-pro-max/provider/manual")) + """',
        '人工材料录入','manual','manual-v1',true,true,'verified',
        '["evidence.manual","calendar.import"]'::jsonb,
        '{"display":"allowed","cache":"allowed","export":"unknown","redistribute":"unknown"}'::jsonb)""")
    # Existing descriptor history is unknown. Register it now, never backdate it.
    import hashlib
    import json
    bind = op.get_bind()
    for row in bind.execute(sa.text("SELECT id,name,kind,adapter_version,configured,enabled,entitlement_status,capabilities,rights FROM providers")).mappings():
        descriptor = {**row, "id": str(row["id"])}
        encoded = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        bind.execute(sa.text("INSERT INTO source_versions(id,source_id,recorded_at,descriptor,content_hash) VALUES (:id,:source_id,clock_timestamp(),CAST(:descriptor AS jsonb),:hash)"),
                     {"id": uuid.uuid4(), "source_id": row["id"], "descriptor": encoded, "hash": hashlib.sha256(encoded.encode()).hexdigest()})


def downgrade():
    raise RuntimeError("Restore a private backup into a separate instance; never drop research data.")
