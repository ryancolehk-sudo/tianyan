"""Stable source identity and append-only collection history; preserve research."""
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = '0007_market_sources'
down_revision = '0006_review_archives'
branch_labels = None
depends_on = None


def upgrade():
    for statement in (Path(__file__).parents[1]/'sql/0007_market_sources.sql').read_text(encoding='utf-8').split('-- statement --'):
        if statement.strip():
            op.execute(sa.text(statement))


def downgrade():
    raise RuntimeError('Restore a backup into a separate empty instance; never erase source or research history.')
