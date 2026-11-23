"""Allow bounded reference tables without rewriting historical evidence."""
from alembic import op

revision = '0010_reference_datasets'
down_revision = '0009_instrument_local_names'
branch_labels = None
depends_on = None


def upgrade():
    # PostgreSQL assigned this name to the original anonymous kind constraint.
    op.drop_constraint('evidence_versions_kind_check', 'evidence_versions', type_='check')
    op.create_check_constraint('evidence_versions_kind_check', 'evidence_versions',
        "kind IN ('manual','news','quote','bar','classification','calendar','reference')")


def downgrade():
    raise RuntimeError('Restore a backup into a separate empty instance; retain collected reference versions.')
