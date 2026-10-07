"""Workspace mutation audit records."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "audit_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("organizer_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_entries"),
        sa.ForeignKeyConstraint(
            ["organizer_id"], ["organizers.id"], name="fk_audit_entries_organizer_id_organizers"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_audit_entries_actor_id_users"),
    )
    op.create_index("ix_audit_entries_organizer_id", "audit_entries", ["organizer_id"])
    op.create_index("ix_audit_entries_actor_id", "audit_entries", ["actor_id"])


def downgrade():
    op.drop_table("audit_entries")
