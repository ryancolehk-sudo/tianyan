"""Locate existing request permits and trusted calendar days without row scans."""
from alembic import op
import sqlalchemy as sa

revision = '0031_collection_gate_indexes'
down_revision = '0030_ths_member_capture_facet'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_audit_action_time', 'audit_events', ['action', 'created_at'])
    op.create_index('ix_evidence_calendar_lookup', 'evidence_versions',
        ['source_id', sa.text("(payload->>'trade_day')"), 'received_at'],
        postgresql_where=sa.text("kind='calendar' AND owner_id IS NULL AND visibility='shared' AND provenance='source_reported'"))


def downgrade():
    op.drop_index('ix_evidence_calendar_lookup', table_name='evidence_versions')
    op.drop_index('ix_audit_action_time', table_name='audit_events')
