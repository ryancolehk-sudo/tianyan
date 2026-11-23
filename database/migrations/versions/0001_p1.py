"""P1 foundation; no research/strategy data or old-system imports."""
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg
from app.config import settings

revision = "0001_p1"
down_revision = None
branch_labels = None
depends_on = None


def ident():
    return sa.Column("id", sa.Uuid(), primary_key=True)


def created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade():
    op.create_table("instance_state", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("app_mode", sa.String(10), nullable=False), sa.CheckConstraint("id = 1"),
        sa.CheckConstraint("app_mode IN ('real','demo')"))
    op.bulk_insert(sa.table("instance_state", sa.column("id", sa.Integer()), sa.column("app_mode")),
        [{"id": 1, "app_mode": settings().app_mode}])
    op.create_table("users", ident(), sa.Column("username", sa.String(80), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False), sa.Column("role", sa.String(20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False), created(), sa.CheckConstraint("role IN ('admin','researcher','viewer')"))
    op.create_table("sessions", ident(), sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False), sa.Column("csrf_token", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("revoked_at", sa.DateTime(timezone=True)), created())
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_table("login_throttles", sa.Column("bucket", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("resets_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_login_throttles_resets_at", "login_throttles", ["resets_at"])
    op.create_table("audit_events", ident(), sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("action", sa.String(80), nullable=False), sa.Column("request_id", sa.Uuid()), created())
    op.create_table("providers", ident(), sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(30), unique=True, nullable=False), sa.Column("adapter_version", sa.String(80), nullable=False),
        sa.Column("configured", sa.Boolean(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("entitlement_status", sa.String(20), nullable=False), sa.Column("capabilities", pg.JSONB(), nullable=False),
        sa.Column("rights", pg.JSONB(), nullable=False))
    op.create_table("instruments", ident(), sa.Column("exchange", sa.String(10), nullable=False),
        sa.Column("symbol", sa.String(6), nullable=False), sa.Column("name", sa.String(200), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False), sa.Column("valid_to", sa.Date()),
        sa.UniqueConstraint("exchange", "symbol", "valid_from"), sa.CheckConstraint("exchange IN ('SSE','SZSE','BSE')"),
        sa.CheckConstraint("symbol ~ '^[0-9]{6}$'"), sa.CheckConstraint("valid_to IS NULL OR valid_to >= valid_from"))
    op.create_index("ix_instruments_symbol", "instruments", ["symbol"])
    op.create_table("jobs", ident(), sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(80), nullable=False), sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("result_ref", pg.JSONB()), sa.Column("error_code", sa.String(80)),
        sa.Column("lease_token", sa.Uuid()), sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        created(), sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("state IN ('queued','running','succeeded','failed','cancelled')"),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 3"))
    op.create_index("ix_jobs_owner_id", "jobs", ["owner_id"])
    op.create_index("ix_jobs_claim", "jobs", ["state", "available_at", "lease_until"])
    op.create_table("worker_heartbeats", sa.Column("worker_id", sa.String(80), primary_key=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False))
    table = sa.table("providers", sa.column("id", sa.Uuid()), sa.column("name"), sa.column("kind"),
        sa.column("adapter_version"), sa.column("configured", sa.Boolean()), sa.column("enabled", sa.Boolean()),
        sa.column("entitlement_status"), sa.column("capabilities", pg.JSONB()), sa.column("rights", pg.JSONB()))
    op.bulk_insert(table, [{"id": uuid.uuid5(uuid.NAMESPACE_URL, "jiulong-pro-max/provider/" + kind),
        "name": name, "kind": kind, "adapter_version": "not-installed", "configured": False, "enabled": False,
        "entitlement_status": "unknown", "capabilities": [],
        "rights": {k: "unknown" for k in ["display", "cache", "export", "redistribute"]}}
        for kind, name in [("market", "行情数据"), ("news", "新闻资讯"), ("calendar", "交易日历"), ("model", "AI 模型")]])


def downgrade():
    # Downgrades are intentionally blocked; restore into a separate instance instead.
    raise RuntimeError("P1 migration is additive. Use an isolated backup restore; do not drop user data.")
