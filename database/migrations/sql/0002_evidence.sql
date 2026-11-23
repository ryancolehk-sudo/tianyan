
CREATE TABLE source_versions (
	id UUID NOT NULL,
	source_id UUID NOT NULL,
	recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
	descriptor JSONB NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(source_id) REFERENCES providers (id)
)

;
-- statement --
CREATE INDEX ix_source_version_cutoff ON source_versions (source_id, recorded_at, id);
-- statement --

CREATE TABLE evidence_versions (
	id UUID NOT NULL,
	source_id UUID NOT NULL,
	owner_id UUID,
	visibility VARCHAR(10) NOT NULL,
	source_key VARCHAR(200) NOT NULL,
	source_revision VARCHAR(200) NOT NULL,
	version_number INTEGER NOT NULL,
	kind VARCHAR(30) NOT NULL,
	instrument_id UUID,
	event_at TIMESTAMP WITH TIME ZONE,
	published_at TIMESTAMP WITH TIME ZONE,
	received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	available_at TIMESTAMP WITH TIME ZONE,
	availability VARCHAR(10) NOT NULL,
	provenance VARCHAR(20) NOT NULL,
	historical_eligible BOOLEAN NOT NULL,
	rights_basis VARCHAR NOT NULL,
	payload JSONB NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	supersedes_id UUID,
	PRIMARY KEY (id),
	CONSTRAINT uq_evidence_source_revision UNIQUE NULLS NOT DISTINCT (source_id, owner_id, source_key, source_revision),
	CONSTRAINT uq_evidence_scope UNIQUE (id, owner_id, source_id, source_key),
	CONSTRAINT uq_evidence_sequence UNIQUE NULLS NOT DISTINCT (source_id, owner_id, source_key, version_number),
	FOREIGN KEY(supersedes_id, owner_id, source_id, source_key) REFERENCES evidence_versions (id, owner_id, source_id, source_key),
	CHECK ((visibility='private' AND owner_id IS NOT NULL) OR (visibility='shared' AND owner_id IS NULL)),
	CHECK (kind IN ('manual','news','quote','bar','classification','calendar')),
	CHECK (availability IN ('known','unknown')),
	CHECK (provenance IN ('manual','source_reported','unverified')),
	CHECK (content_hash ~ '^[a-f0-9]{64}$'),
	CHECK (version_number > 0),
	CHECK (available_at IS NULL OR available_at >= received_at),
	FOREIGN KEY(source_id) REFERENCES providers (id),
	FOREIGN KEY(owner_id) REFERENCES users (id),
	FOREIGN KEY(instrument_id) REFERENCES instruments (id),
	UNIQUE (supersedes_id)
)

;
-- statement --
CREATE INDEX ix_evidence_owner_cutoff ON evidence_versions (owner_id, source_id, source_key, available_at);
-- statement --

CREATE TABLE source_health (
	id UUID NOT NULL,
	owner_id UUID NOT NULL,
	source_id UUID NOT NULL,
	checked_at TIMESTAMP WITH TIME ZONE NOT NULL,
	status VARCHAR(20) NOT NULL,
	last_event_at TIMESTAMP WITH TIME ZONE,
	last_received_at TIMESTAMP WITH TIME ZONE,
	coverage JSONB NOT NULL,
	reason VARCHAR(300) NOT NULL,
	PRIMARY KEY (id),
	CHECK (status IN ('ready','unknown','unavailable','error')),
	FOREIGN KEY(owner_id) REFERENCES users (id),
	FOREIGN KEY(source_id) REFERENCES providers (id)
)

;
-- statement --
CREATE INDEX ix_source_health_owner_time ON source_health (owner_id, checked_at, id);
-- statement --

CREATE TABLE calendar_versions (
	id UUID NOT NULL,
	exchange VARCHAR(10) NOT NULL,
	version VARCHAR(200) NOT NULL,
	source_ref VARCHAR(2000) NOT NULL,
	rights_basis VARCHAR NOT NULL,
	coverage_start DATE NOT NULL,
	coverage_end DATE NOT NULL,
	recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
	author_id UUID NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (exchange, version),
	CHECK (coverage_end >= coverage_start),
	CHECK (exchange IN ('SSE','SZSE','BSE')),
	CHECK (content_hash ~ '^[a-f0-9]{64}$'),
	FOREIGN KEY(author_id) REFERENCES users (id)
)

;
-- statement --

CREATE TABLE trading_sessions (
	calendar_id UUID NOT NULL,
	trade_day DATE NOT NULL,
	is_open BOOLEAN NOT NULL,
	segments JSONB NOT NULL,
	PRIMARY KEY (calendar_id, trade_day),
	FOREIGN KEY(calendar_id) REFERENCES calendar_versions (id)
)

;
-- statement --

CREATE TABLE snapshots (
	id UUID NOT NULL,
	owner_id UUID NOT NULL,
	as_of_time TIMESTAMP WITH TIME ZONE NOT NULL,
	recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
	knowledge_mode VARCHAR(20) NOT NULL,
	state VARCHAR(20) NOT NULL,
	selection JSONB NOT NULL,
	manifest JSONB,
	manifest_hash VARCHAR(64),
	error_code VARCHAR(80),
	PRIMARY KEY (id),
	UNIQUE (id, owner_id),
	CHECK (state IN ('pending','ready','failed')),
	CHECK (knowledge_mode='observed'),
	CHECK (as_of_time <= recorded_at),
	CHECK ((state='ready' AND manifest IS NOT NULL AND manifest_hash ~ '^[a-f0-9]{64}$') OR (state<>'ready' AND manifest IS NULL AND manifest_hash IS NULL)),
	FOREIGN KEY(owner_id) REFERENCES users (id)
)

;
-- statement --
CREATE INDEX ix_snapshots_owner_time ON snapshots (owner_id, recorded_at, id);
-- statement --

CREATE TABLE snapshot_items (
	snapshot_id UUID NOT NULL,
	evidence_version_id UUID NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	PRIMARY KEY (snapshot_id, evidence_version_id),
	FOREIGN KEY(snapshot_id) REFERENCES snapshots (id),
	FOREIGN KEY(evidence_version_id) REFERENCES evidence_versions (id)
)

;
-- statement --

CREATE TABLE idempotency_keys (
	owner_id UUID NOT NULL,
	method VARCHAR(10) NOT NULL,
	route VARCHAR(200) NOT NULL,
	key VARCHAR(128) NOT NULL,
	payload_hash VARCHAR(64) NOT NULL,
	response_json JSONB NOT NULL,
	status_code INTEGER NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (owner_id, method, route, key),
	FOREIGN KEY(owner_id) REFERENCES users (id)
)

;
-- statement --
CREATE INDEX ix_idempotency_keys_expires_at ON idempotency_keys (expires_at);
-- statement --
