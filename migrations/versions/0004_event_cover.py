"""Event cover image URL."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("cover_url", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("events", "cover_url")
