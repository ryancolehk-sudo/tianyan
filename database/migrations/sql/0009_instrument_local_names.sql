ALTER TABLE instruments ADD COLUMN local_name varchar(200) CHECK(local_name IS NULL OR length(btrim(local_name))>0);
-- statement --
-- Recover verifiable pre-0008 quote claims from immutable machine evidence.
-- No catalogue provenance or upstream timestamp is invented for legacy rows.
INSERT INTO instrument_origins(id,instrument_id,source_id,origin_kind,observed_name,
 first_observed_at,last_observed_at,last_content_hash,source_version_id)
SELECT gen_random_uuid(),e.instrument_id,e.source_id,'quote',
 coalesce(nullif(e.payload->>'instrument_name',''),i.name),
 (SELECT min(old.received_at) FROM evidence_versions old WHERE old.instrument_id=e.instrument_id
  AND old.source_id=e.source_id AND old.kind='quote' AND old.provenance='source_reported'),
 e.received_at,e.content_hash,
 (SELECT v.id FROM source_versions v WHERE v.source_id=e.source_id AND v.recorded_at<=e.received_at
  ORDER BY v.recorded_at DESC,v.id DESC LIMIT 1)
FROM (SELECT DISTINCT ON (instrument_id,source_id) * FROM evidence_versions
 WHERE instrument_id IS NOT NULL AND kind='quote' AND provenance='source_reported'
 AND visibility='shared' AND owner_id IS NULL ORDER BY instrument_id,source_id,received_at DESC,id DESC) e
JOIN instruments i ON i.id=e.instrument_id
ON CONFLICT(instrument_id,source_id) DO NOTHING;
-- statement --
-- Preserve the existing local/legacy baseline before a later source claims it.
-- Source-backed identities do not receive this independent fallback.
UPDATE instruments i SET local_name=i.name
WHERE NOT EXISTS(SELECT 1 FROM instrument_origins o WHERE o.instrument_id=i.id);
