"""Record which source claims each shared instrument so reads can recheck rights."""
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = '0008_instrument_origins'
down_revision = '0007_market_sources'
branch_labels = None
depends_on = None


def upgrade():
    for statement in (Path(__file__).parents[1]/'sql/0008_instrument_origins.sql').read_text(encoding='utf-8').split('-- statement --'):
        if statement.strip():
            op.execute(sa.text(statement))


def downgrade():
    raise RuntimeError('Restore a backup into a separate empty instance; never erase instrument provenance.')
