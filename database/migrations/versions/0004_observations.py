"""Append-only auction observations with fixed scenario and temporal scope."""
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = '0004_observations'
down_revision = '0003_research'
branch_labels = None
depends_on = None


def upgrade():
    ddl = (Path(__file__).parents[1] / 'sql/0004_observations.sql').read_text(encoding='utf-8')
    for statement in ddl.split('-- statement --'):
        if statement.strip():
            op.execute(sa.text(statement))


def downgrade():
    raise RuntimeError('Restore a private backup into a separate empty instance; never delete observations.')
