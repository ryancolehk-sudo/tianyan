CREATE TABLE auction_observations (
  id uuid PRIMARY KEY, owner_id uuid NOT NULL,
  plan_revision_id uuid NOT NULL, scenario_id uuid NOT NULL, snapshot_id uuid NOT NULL,
  observed_at timestamptz NOT NULL, recorded_at timestamptz NOT NULL,
  phase varchar(5) NOT NULL CHECK (phase IN ('09:15','09:20','09:25')),
  status varchar(20) NOT NULL CHECK (status IN ('observed','unavailable')),
  facts jsonb NOT NULL, commentary text, evidence_refs jsonb NOT NULL,
  content_hash varchar(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
  FOREIGN KEY(scenario_id,plan_revision_id,owner_id) REFERENCES plan_scenarios(id,plan_revision_id,owner_id),
  FOREIGN KEY(snapshot_id,owner_id) REFERENCES snapshots(id,owner_id),
  CHECK (observed_at <= recorded_at),
  CHECK (jsonb_typeof(facts)='array' AND jsonb_typeof(evidence_refs)='array'),
  CHECK (jsonb_array_length(facts)<=100 AND jsonb_array_length(evidence_refs)<=100),
  CHECK ((status='unavailable' AND facts='[]' AND evidence_refs='[]' AND commentary IS NOT NULL AND length(trim(commentary))>0)
    OR (status='observed' AND jsonb_array_length(facts)>0 AND jsonb_array_length(evidence_refs)>0))
);
-- statement --
CREATE INDEX ix_observation_plan_recorded ON auction_observations(owner_id,plan_revision_id,recorded_at,id);
-- statement --
CREATE FUNCTION protect_observation_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d research_documents; r document_revisions; s snapshots; local_at timestamp;
BEGIN
  SELECT * INTO STRICT r FROM document_revisions WHERE id=NEW.plan_revision_id AND owner_id=NEW.owner_id;
  SELECT * INTO STRICT d FROM research_documents WHERE id=r.document_id FOR UPDATE;
  SELECT * INTO STRICT s FROM snapshots WHERE id=NEW.snapshot_id AND owner_id=NEW.owner_id;
  local_at := NEW.observed_at AT TIME ZONE 'Asia/Shanghai';
  IF d.kind<>'plan' OR d.target_trade_day IS DISTINCT FROM local_at::date
    OR local_at::time <> NEW.phase::time OR NEW.observed_at>NEW.recorded_at
    OR NOT EXISTS(SELECT 1 FROM publication_events WHERE revision_id=r.id AND kind='published' AND recorded_at<NEW.observed_at)
    OR EXISTS(SELECT 1 FROM publication_events WHERE revision_id=r.id AND kind='withdrawn') THEN
    RAISE EXCEPTION 'invalid observation plan/time' USING ERRCODE='23514';
  END IF;
  IF s.state<>'ready' OR s.id=r.snapshot_id OR s.recorded_at>NEW.recorded_at OR s.as_of_time>NEW.recorded_at
    OR s.as_of_time<NEW.observed_at OR (s.as_of_time AT TIME ZONE 'Asia/Shanghai')::date<>d.target_trade_day THEN
    RAISE EXCEPTION 'invalid observation snapshot' USING ERRCODE='23514';
  END IF;
  IF NOT EXISTS(SELECT 1 FROM trading_sessions ts CROSS JOIN LATERAL jsonb_array_elements(ts.segments) seg
    WHERE ts.calendar_id=r.calendar_version_id AND ts.trade_day=d.target_trade_day AND ts.is_open
      AND seg->>'kind'='auction' AND (seg->>'start')::time<=local_at::time AND local_at::time<=(seg->>'end')::time) THEN
    RAISE EXCEPTION 'auction calendar unavailable' USING ERRCODE='23514';
  END IF;
  IF NEW.status='observed' THEN
    IF (s.manifest->'quality'->>'status') IS NULL OR s.manifest->'quality'->>'status' NOT IN ('complete','partial') THEN
      RAISE EXCEPTION 'observation evidence unavailable' USING ERRCODE='23514';
    END IF;
    IF EXISTS(SELECT 1 FROM jsonb_array_elements_text(NEW.evidence_refs) ref
      WHERE NOT EXISTS(SELECT 1 FROM snapshot_items si JOIN evidence_versions e ON e.id=si.evidence_version_id
        WHERE si.snapshot_id=s.id AND e.id=ref::uuid AND e.historical_eligible AND e.provenance<>'unverified' AND e.availability='known' AND e.available_at<=NEW.observed_at
          AND (e.published_at IS NULL OR e.published_at<=NEW.observed_at)
          AND e.received_at<=NEW.observed_at AND (e.event_at IS NULL OR e.event_at<=NEW.observed_at))) THEN
      RAISE EXCEPTION 'observation material not known at checkpoint' USING ERRCODE='23514';
    END IF;
    IF EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.facts) f
      WHERE jsonb_typeof(f) IS DISTINCT FROM 'object' OR jsonb_typeof(f->'field') IS DISTINCT FROM 'string'
        OR jsonb_typeof(f->'value') IS DISTINCT FROM 'string' OR jsonb_typeof(f->'unit') IS DISTINCT FROM 'string'
        OR length(trim(f->>'field'))=0 OR length(trim(f->>'value'))=0 OR length(trim(f->>'unit'))=0
        OR f->>'evidence_ref' IS NULL OR NOT (NEW.evidence_refs ? (f->>'evidence_ref'))) THEN
      RAISE EXCEPTION 'observation facts lack explicit evidence' USING ERRCODE='23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;
-- statement --
CREATE TRIGGER observation_scope BEFORE INSERT ON auction_observations FOR EACH ROW EXECUTE FUNCTION protect_observation_insert();
-- statement --
CREATE TRIGGER immutable_observation BEFORE UPDATE OR DELETE ON auction_observations FOR EACH ROW EXECUTE FUNCTION reject_research_mutation();
-- statement --
GRANT SELECT, INSERT ON auction_observations TO jlpm_app;
-- statement --
REVOKE UPDATE, DELETE ON auction_observations FROM jlpm_app;
