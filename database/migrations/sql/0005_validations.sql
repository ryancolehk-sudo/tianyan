ALTER TABLE auction_observations ADD CONSTRAINT uq_observation_validation_scope UNIQUE(id,owner_id,plan_revision_id,scenario_id);
-- statement --
CREATE TABLE validations (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL, plan_revision_id uuid NOT NULL, scenario_id uuid NOT NULL,
 observation_id uuid, snapshot_id uuid NOT NULL, outcome varchar(20) NOT NULL CHECK(outcome IN ('supported','refuted','inconclusive')),
 reason text NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 20000), evidence_refs jsonb NOT NULL,
 supersedes_id uuid UNIQUE, recorded_at timestamptz NOT NULL,
 content_hash varchar(64) NOT NULL CHECK(content_hash ~ '^[a-f0-9]{64}$'),
 UNIQUE(id,owner_id,plan_revision_id,scenario_id),
 FOREIGN KEY(scenario_id,plan_revision_id,owner_id) REFERENCES plan_scenarios(id,plan_revision_id,owner_id),
 FOREIGN KEY(snapshot_id,owner_id) REFERENCES snapshots(id,owner_id),
 FOREIGN KEY(observation_id,owner_id,plan_revision_id,scenario_id) REFERENCES auction_observations(id,owner_id,plan_revision_id,scenario_id),
 FOREIGN KEY(supersedes_id,owner_id,plan_revision_id,scenario_id) REFERENCES validations(id,owner_id,plan_revision_id,scenario_id),
 CHECK(supersedes_id IS NULL OR supersedes_id<>id),
 CHECK(jsonb_typeof(evidence_refs)='array' AND jsonb_array_length(evidence_refs)<=100),
 CHECK(outcome='inconclusive' OR jsonb_array_length(evidence_refs)>0)
);
-- statement --
CREATE UNIQUE INDEX ux_validation_root ON validations(owner_id,plan_revision_id,scenario_id) WHERE supersedes_id IS NULL;
-- statement --
CREATE INDEX ix_validation_plan_recorded ON validations(owner_id,plan_revision_id,recorded_at,id);
-- statement --
CREATE FUNCTION protect_validation_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d research_documents; r document_revisions; s snapshots; close_at timestamptz; previous validations;
BEGIN
 SELECT * INTO STRICT r FROM document_revisions WHERE id=NEW.plan_revision_id AND owner_id=NEW.owner_id;
 SELECT * INTO STRICT d FROM research_documents WHERE id=r.document_id FOR UPDATE;
 SELECT * INTO STRICT s FROM snapshots WHERE id=NEW.snapshot_id AND owner_id=NEW.owner_id;
 SELECT (d.target_trade_day+max((seg->>'end')::time)) AT TIME ZONE 'Asia/Shanghai' INTO close_at
   FROM trading_sessions ts CROSS JOIN LATERAL jsonb_array_elements(ts.segments) seg
   WHERE ts.calendar_id=r.calendar_version_id AND ts.trade_day=d.target_trade_day AND ts.is_open;
 IF close_at IS NULL OR NOT EXISTS(SELECT 1 FROM trading_sessions ts CROSS JOIN LATERAL jsonb_array_elements(ts.segments) seg
   WHERE ts.calendar_id=r.calendar_version_id AND ts.trade_day=d.target_trade_day AND ts.is_open AND seg->>'kind'='continuous') THEN
   RAISE EXCEPTION 'validation close unavailable' USING ERRCODE='23514';
 END IF;
 IF d.kind<>'plan' OR NEW.recorded_at<close_at
   OR NOT EXISTS(SELECT 1 FROM publication_events WHERE revision_id=r.id AND kind='published' AND recorded_at<close_at)
   OR EXISTS(SELECT 1 FROM publication_events WHERE revision_id=r.id AND kind='withdrawn') THEN
   RAISE EXCEPTION 'invalid validation plan/time' USING ERRCODE='23514';
 END IF;
 IF s.state<>'ready' OR s.id=r.snapshot_id OR s.recorded_at>NEW.recorded_at OR s.as_of_time>NEW.recorded_at OR s.as_of_time<close_at THEN
   RAISE EXCEPTION 'invalid validation snapshot' USING ERRCODE='23514';
 END IF;
 IF NEW.observation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM auction_observations o WHERE o.id=NEW.observation_id
   AND o.owner_id=NEW.owner_id AND o.plan_revision_id=r.id AND o.scenario_id=NEW.scenario_id AND o.recorded_at<=NEW.recorded_at) THEN
   RAISE EXCEPTION 'invalid validation observation' USING ERRCODE='23514';
 END IF;
 IF NEW.outcome<>'inconclusive' AND (s.manifest->'quality'->>'status' IS NULL OR s.manifest->'quality'->>'status' NOT IN ('complete','partial')) THEN
   RAISE EXCEPTION 'validation evidence unavailable' USING ERRCODE='23514';
 END IF;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements_text(NEW.evidence_refs) ref WHERE NOT EXISTS(
   SELECT 1 FROM snapshot_items si JOIN evidence_versions e ON e.id=si.evidence_version_id WHERE si.snapshot_id=s.id AND e.id=ref::uuid
    AND e.historical_eligible AND e.provenance<>'unverified' AND e.availability='known' AND e.available_at<=s.as_of_time
    AND e.received_at<=s.as_of_time AND (e.published_at IS NULL OR e.published_at<=s.as_of_time) AND (e.event_at IS NULL OR e.event_at<=s.as_of_time))) THEN
   RAISE EXCEPTION 'validation material unavailable at cutoff' USING ERRCODE='23514';
 END IF;
 SELECT * INTO previous FROM validations v WHERE v.owner_id=NEW.owner_id AND v.plan_revision_id=NEW.plan_revision_id AND v.scenario_id=NEW.scenario_id
   AND NOT EXISTS(SELECT 1 FROM validations n WHERE n.supersedes_id=v.id);
 IF previous.id IS DISTINCT FROM NEW.supersedes_id OR (previous.id IS NOT NULL AND previous.recorded_at>=NEW.recorded_at) THEN
   RAISE EXCEPTION 'validation correction must extend current end' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
-- statement --
CREATE TRIGGER validation_scope BEFORE INSERT ON validations FOR EACH ROW EXECUTE FUNCTION protect_validation_insert();
-- statement --
CREATE TRIGGER immutable_validation BEFORE UPDATE OR DELETE ON validations FOR EACH ROW EXECUTE FUNCTION reject_research_mutation();
-- statement --
GRANT SELECT,INSERT ON validations TO jlpm_app;
-- statement --
REVOKE UPDATE,DELETE ON validations FROM jlpm_app;
