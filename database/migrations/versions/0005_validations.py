"""Append-only scenario validation correction chains."""
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = '0005_validations'
down_revision = '0004_observations'
branch_labels = None
depends_on = None


def upgrade():
    for statement in (Path(__file__).parents[1]/'sql/0005_validations.sql').read_text(encoding='utf-8').split('-- statement --'):
        if statement.strip(): op.execute(sa.text(statement))


def downgrade():
    raise RuntimeError('Restore a private backup into a separate empty instance; never delete validation history.')
