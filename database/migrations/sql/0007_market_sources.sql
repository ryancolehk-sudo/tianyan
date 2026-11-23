ALTER TABLE providers DROP CONSTRAINT providers_kind_key;
-- statement --
ALTER TABLE providers ADD COLUMN family varchar(80) UNIQUE,
 ADD CONSTRAINT ck_provider_family CHECK(family IS NULL OR family ~ '^[a-z][a-z0-9_.-]{1,79}$');
-- statement --
ALTER TABLE instruments DROP CONSTRAINT instruments_exchange_symbol_valid_from_key;
-- statement --
ALTER TABLE instruments ALTER COLUMN valid_from DROP NOT NULL,
 ADD COLUMN asset_type varchar(20) NOT NULL DEFAULT 'stock',
 ADD COLUMN first_received_at timestamptz,
 ADD CONSTRAINT ck_instrument_asset_type CHECK(asset_type IN ('stock','index','etf','convertible')),
 ADD CONSTRAINT uq_instrument_identity_period UNIQUE NULLS NOT DISTINCT(exchange,asset_type,symbol,valid_from);
-- statement --
CREATE TABLE source_settings (
 source_id uuid PRIMARY KEY REFERENCES providers(id),
 owner_id uuid NOT NULL REFERENCES users(id),
 rights_basis text NOT NULL,
 config jsonb NOT NULL,
 interval_seconds integer NOT NULL CHECK(interval_seconds BETWEEN 15 AND 86400),
 configured_at timestamptz NOT NULL,
 next_run_at timestamptz NOT NULL,
 paused_reason varchar(80),
 CHECK(jsonb_typeof(config)='object')
);
-- statement --
CREATE INDEX ix_source_next_run ON source_settings(next_run_at,source_id);
-- statement --
CREATE TABLE data_fetch_runs (
 id uuid PRIMARY KEY,
 source_id uuid NOT NULL REFERENCES providers(id),
 capability varchar(80) NOT NULL,
 job_id uuid REFERENCES jobs(id),
 started_at timestamptz NOT NULL,
 received_at timestamptz NOT NULL,
 status varchar(20) NOT NULL CHECK(status IN ('complete','partial','empty','unavailable','error')),
 reason varchar(80),
 expected integer CHECK(expected IS NULL OR expected>=0),
 accepted integer CHECK(accepted IS NULL OR accepted>=0),
 rejected integer CHECK(rejected IS NULL OR rejected>=0),
 duplicates integer CHECK(duplicates IS NULL OR duplicates>=0),
 elapsed_ms integer CHECK(elapsed_ms IS NULL OR elapsed_ms>=0),
 request_count integer NOT NULL CHECK(request_count>=0),
 cost_cny numeric CHECK(cost_cny IS NULL OR cost_cny>=0),
 body_hash varchar(64) CHECK(body_hash IS NULL OR body_hash ~ '^[a-f0-9]{64}$'),
 CHECK(received_at>=started_at)
);
-- statement --
CREATE INDEX ix_fetch_source_capability_time ON data_fetch_runs(source_id,capability,received_at,id);
-- statement --
CREATE TRIGGER immutable_fetch_run BEFORE UPDATE OR DELETE ON data_fetch_runs FOR EACH ROW EXECUTE FUNCTION reject_research_mutation();
-- statement --
GRANT SELECT,INSERT,UPDATE ON source_settings TO jlpm_app;
-- statement --
GRANT SELECT,INSERT ON data_fetch_runs TO jlpm_app;
-- statement --
REVOKE UPDATE,DELETE ON data_fetch_runs FROM jlpm_app;
