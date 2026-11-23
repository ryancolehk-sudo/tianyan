CREATE TABLE review_archives (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id),
 source_identity varchar(64) NOT NULL, source_repository varchar(100) NOT NULL, source_commit varchar(40) NOT NULL,
 based_on_trade_day date NOT NULL, target_trade_day date NOT NULL, source_version integer NOT NULL,
 source_stage varchar(100) NOT NULL, evidence_cutoff_at timestamptz NOT NULL,
 source_generated_at timestamptz NOT NULL, imported_at timestamptz NOT NULL,
 payload jsonb NOT NULL, summary_text text, content_hash varchar(64) NOT NULL,
 UNIQUE(owner_id,source_identity),
 CHECK(target_trade_day > based_on_trade_day), CHECK(source_version BETWEEN 1 AND 999999),
 CHECK(evidence_cutoff_at <= source_generated_at AND source_generated_at <= imported_at),
 CHECK(content_hash ~ '^[a-f0-9]{64}$' AND source_identity ~ '^[a-f0-9]{64}$'),
 CHECK(source_commit ~ '^[a-f0-9]{40}$'), CHECK(jsonb_typeof(payload)='object')
);
-- statement --
CREATE INDEX ix_archive_owner_day_version ON review_archives(owner_id,target_trade_day,source_version,imported_at,id);
-- statement --
CREATE TRIGGER immutable_review_archive BEFORE UPDATE OR DELETE ON review_archives FOR EACH ROW EXECUTE FUNCTION reject_research_mutation();
-- statement --
GRANT SELECT,INSERT ON review_archives TO jlpm_app;
-- statement --
REVOKE UPDATE,DELETE ON review_archives FROM jlpm_app;
