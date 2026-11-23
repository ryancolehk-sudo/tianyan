"""Owner-scoped versioned research screens and immutable evaluations/events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019_screening"
down_revision = "0018_settings_tomorrow_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "screen_rules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("transition_state", JSONB(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(80)),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1"),
    )
    op.create_index("ix_screen_rules_owner_id", "screen_rules", ["owner_id"])
    op.create_index("ix_screen_rules_next_run_at", "screen_rules", ["next_run_at"])
    op.create_table(
        "screen_rule_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "rule_id", sa.Uuid(), sa.ForeignKey("screen_rules.id"), nullable=False
        ),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("rule_id", "version", name="uq_screen_rule_version"),
    )
    op.create_table(
        "screen_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "rule_id", sa.Uuid(), sa.ForeignKey("screen_rules.id"), nullable=False
        ),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("rule_name", sa.String(80), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("trade_day", sa.Date(), nullable=False),
        sa.Column("as_of_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("engine_version", sa.String(40), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("data_revision", sa.String(64), nullable=False),
        sa.Column("inputs", JSONB(), nullable=False),
        sa.Column("source_refs", JSONB(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("reasons", JSONB(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "rule_id", "rule_version", "input_hash", "mode", name="uq_screen_run_input"
        ),
        sa.CheckConstraint("mode IN ('review','observed','monitor')"),
    )
    op.create_index(
        "ix_screen_runs_owner_time", "screen_runs", ["owner_id", "created_at"]
    )
    op.create_table(
        "screen_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "rule_id", sa.Uuid(), sa.ForeignKey("screen_rules.id"), nullable=False
        ),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("screen_runs.id"), nullable=False),
        sa.Column("rule_name", sa.String(80), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("source_time", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "run_id", "code", "direction", name="uq_screen_event_transition"
        ),
        sa.CheckConstraint("direction IN ('entered','exited')"),
    )
    op.create_index(
        "ix_screen_events_owner_time", "screen_events", ["owner_id", "created_at"]
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON screen_rules TO jlpm_app")
    op.execute(
        "GRANT SELECT, INSERT ON screen_rule_versions, screen_runs, screen_events TO jlpm_app"
    )
    op.execute("REVOKE UPDATE, DELETE ON screen_rule_versions, screen_runs, screen_events FROM jlpm_app")
    op.execute("REVOKE DELETE ON screen_rules FROM jlpm_app")


def downgrade():
    raise RuntimeError(
        "Retain immutable research history; use the schema-compatible previous application."
    )
