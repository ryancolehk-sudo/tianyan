CREATE TABLE instrument_origins (
 id uuid PRIMARY KEY,
 instrument_id uuid NOT NULL REFERENCES instruments(id),
 source_id uuid NOT NULL REFERENCES providers(id),
 origin_kind varchar(20) NOT NULL CHECK(origin_kind IN ('catalog','quote')),
 observed_name varchar(200) NOT NULL CHECK(length(btrim(observed_name))>0),
 first_observed_at timestamptz NOT NULL,
 last_observed_at timestamptz NOT NULL,
 last_content_hash varchar(64) NOT NULL CHECK(last_content_hash ~ '^[a-f0-9]{64}$'),
 source_version_id uuid REFERENCES source_versions(id),
 CONSTRAINT uq_instrument_origin_source UNIQUE(instrument_id,source_id),
 CHECK(last_observed_at>=first_observed_at)
);
-- statement --
CREATE INDEX ix_instrument_origins_instrument_id ON instrument_origins(instrument_id);
-- statement --
CREATE INDEX ix_instrument_origins_source_id ON instrument_origins(source_id);
-- statement --
GRANT SELECT,INSERT,UPDATE ON instrument_origins TO jlpm_app;
-- statement --
REVOKE DELETE ON instrument_origins FROM jlpm_app;
