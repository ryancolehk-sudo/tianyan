
CREATE TABLE research_documents (
	id UUID NOT NULL,
	owner_id UUID NOT NULL,
	kind VARCHAR(20) NOT NULL,
	source_trade_day DATE NOT NULL,
	target_trade_day DATE,
	head_revision_number INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (id, owner_id),
	CHECK (kind IN ('review','direction','plan')),
	CHECK (head_revision_number > 0),
	CHECK ((kind='plan' AND target_trade_day IS NOT NULL AND target_trade_day > source_trade_day) OR (kind<>'plan' AND target_trade_day IS NULL)),
	FOREIGN KEY(owner_id) REFERENCES users (id)
)

;
-- statement --
CREATE INDEX ix_research_owner_created ON research_documents (owner_id, created_at, id);
-- statement --

CREATE TABLE document_revisions (
	id UUID NOT NULL,
	document_id UUID NOT NULL,
	owner_id UUID NOT NULL,
	revision_number INTEGER NOT NULL,
	based_on_revision_id UUID,
	title VARCHAR(200) NOT NULL,
	snapshot_id UUID NOT NULL,
	calendar_version_id UUID,
	content JSONB NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	author_id UUID NOT NULL,
	recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (document_id, revision_number),
	UNIQUE (id, owner_id),
	UNIQUE (id, owner_id, document_id),
	FOREIGN KEY(document_id, owner_id) REFERENCES research_documents (id, owner_id),
	FOREIGN KEY(snapshot_id, owner_id) REFERENCES snapshots (id, owner_id),
	FOREIGN KEY(based_on_revision_id, owner_id, document_id) REFERENCES document_revisions (id, owner_id, document_id),
	CHECK (revision_number > 0),
	CHECK (content_hash ~ '^[a-f0-9]{64}$'),
	FOREIGN KEY(calendar_version_id) REFERENCES calendar_versions (id),
	FOREIGN KEY(author_id) REFERENCES users (id)
)

;
-- statement --
CREATE INDEX ix_revision_document_time ON document_revisions (document_id, recorded_at);
-- statement --

CREATE TABLE revision_links (
	child_revision_id UUID NOT NULL,
	parent_revision_id UUID NOT NULL,
	owner_id UUID NOT NULL,
	position INTEGER NOT NULL,
	relation VARCHAR(30) NOT NULL,
	PRIMARY KEY (child_revision_id, parent_revision_id),
	FOREIGN KEY(child_revision_id, owner_id) REFERENCES document_revisions (id, owner_id),
	FOREIGN KEY(parent_revision_id, owner_id) REFERENCES document_revisions (id, owner_id),
	UNIQUE (child_revision_id, position),
	CHECK (child_revision_id <> parent_revision_id),
	CHECK (relation IN ('review_direction','direction_plan'))
)

;
-- statement --

CREATE TABLE publication_events (
	id UUID NOT NULL,
	revision_id UUID NOT NULL,
	owner_id UUID NOT NULL,
	actor_id UUID NOT NULL,
	kind VARCHAR(20) NOT NULL,
	recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
	reason VARCHAR(2000),
	PRIMARY KEY (id),
	FOREIGN KEY(revision_id, owner_id) REFERENCES document_revisions (id, owner_id),
	UNIQUE (revision_id, kind),
	CHECK (kind IN ('published','withdrawn')),
	CHECK (actor_id=owner_id),
	CHECK (kind<>'withdrawn' OR (reason IS NOT NULL AND length(trim(reason)) > 0)),
	FOREIGN KEY(actor_id) REFERENCES users (id)
)

;
-- statement --
CREATE INDEX ix_publication_revision_time ON publication_events (revision_id, recorded_at, id);
-- statement --

CREATE TABLE plan_scenarios (
	id UUID NOT NULL,
	plan_revision_id UUID NOT NULL,
	owner_id UUID NOT NULL,
	position INTEGER NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(plan_revision_id, owner_id) REFERENCES document_revisions (id, owner_id),
	UNIQUE (id, plan_revision_id, owner_id),
	UNIQUE (plan_revision_id, position)
)

;
-- statement --
ALTER TABLE research_documents ADD CONSTRAINT fk_document_head FOREIGN KEY(id, head_revision_number) REFERENCES document_revisions (document_id, revision_number) DEFERRABLE INITIALLY DEFERRED;
-- statement --
