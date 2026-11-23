"""Private immutable legacy review output, never active plans or market evidence."""
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0006_review_archives"
down_revision = "0005_validations"
branch_labels = None
depends_on = None


def upgrade():
    for statement in (Path(__file__).parents[1] / "sql/0006_review_archives.sql").read_text(encoding="utf-8").split("-- statement --"):
        if statement.strip():
            op.execute(sa.text(statement))


def downgrade():
    raise RuntimeError("Restore a private backup to a separate empty instance; do not delete imported research history.")
