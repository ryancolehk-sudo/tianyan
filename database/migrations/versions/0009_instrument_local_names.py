"""Preserve local identities and avoid displaying names from revoked sources."""
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = '0009_instrument_local_names'
down_revision = '0008_instrument_origins'
branch_labels = None
depends_on = None


def upgrade():
    for statement in (Path(__file__).parents[1]/'sql/0009_instrument_local_names.sql').read_text(encoding='utf-8').split('-- statement --'):
        if statement.strip():
            op.execute(sa.text(statement))


def downgrade():
    raise RuntimeError('Restore into a separate empty instance; never erase instrument provenance.')
