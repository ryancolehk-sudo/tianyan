"""Owner-scoped official ChatGPT registrations and durable single-use OAuth attempts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0015_chatgpt_accounts"
down_revision = "0014_data_center"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("service_connections", sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id")))
    op.create_table("chatgpt_installation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("host_id", sa.String(80), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1"))
    op.create_table("chatgpt_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("issuer", sa.String(200), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("issuer", "subject"))
    op.create_table("chatgpt_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("connection_id", sa.Uuid(), sa.ForeignKey("service_connections.id"), nullable=False, unique=True),
        sa.Column("issuer", sa.String(200), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("client_id", sa.String(200), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("secret_ref", sa.String(200)),
        sa.Column("scopes", pg.JSONB(), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("use_for_ai", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("welcome_seen_at", sa.DateTime(timezone=True)),
        sa.Column("remote_revocation_unconfirmed", sa.Boolean(), nullable=False),
        sa.Column("refresh_started_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_authorized_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("issuer", "subject", "client_id"), sa.CheckConstraint("revision > 0"),
        sa.CheckConstraint("state IN ('connected','identity_only','disconnected','refreshing','refresh_unknown','reauthorization_required','client_error')"))
    op.create_index("ix_chatgpt_accounts_owner", "chatgpt_accounts", ["owner_id", "created_at"])
    op.create_index("uq_chatgpt_accounts_selected_owner", "chatgpt_accounts", ["owner_id"], unique=True, postgresql_where=sa.text("use_for_ai"))
    op.create_table("chatgpt_authorizations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column("secret_ref", sa.String(200)),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("sessions.id", ondelete="SET NULL")),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("chatgpt_accounts.id")),
        sa.Column("client_id", sa.String(200)),
        sa.Column("redirect_uri", sa.String(2000), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("ix_chatgpt_authorizations_expires_at", "chatgpt_authorizations", ["expires_at"])
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON chatgpt_installation, chatgpt_identities, chatgpt_accounts, chatgpt_authorizations TO jlpm_app")


def downgrade():
    raise RuntimeError("Disconnect ChatGPT and use the schema-compatible previous application; retain local identities and historical results.")
