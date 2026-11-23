"""Explicit capability routes and derived-cache invalidation."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '0020_data_routes'
down_revision = '0019_screening'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('data_routes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('choices', JSONB(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('id = 1'), sa.CheckConstraint('revision > 0'))
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON data_routes TO jlpm_app')
    # Routes change both server projections and the authenticated browser scope.
    op.execute("""CREATE FUNCTION data_routes_changed() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN PERFORM bump_read_dependency('authorization');
      PERFORM bump_read_dependency('catalog'); RETURN NULL; END $$""")
    op.execute('CREATE TRIGGER data_routes_changed AFTER INSERT OR UPDATE OR DELETE ON data_routes '
               'FOR EACH ROW EXECUTE FUNCTION data_routes_changed()')


def downgrade():
    raise RuntimeError('Use a schema-compatible previous application; preserve source choices and evidence.')
