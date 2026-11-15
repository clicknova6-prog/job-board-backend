"""drop affiliate_links table

The affiliate/promo redirect system is retired: all traffic now comes from our
own job board sites and the Apply button links straight to ``jobs.apply_url``.

Revision ID: a1f0c2d4e6b8
Revises: 7c1a3f0e9b2d
Create Date: 2026-11-15 10:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f0c2d4e6b8"
down_revision: str | Sequence[str] | None = "7c1a3f0e9b2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the affiliate_links table and its index."""
    op.drop_index("ix_affiliate_links_short_hash", table_name="affiliate_links")
    op.drop_table("affiliate_links")


def downgrade() -> None:
    """Recreate the empty affiliate_links table (data is not restored)."""
    op.create_table(
        "affiliate_links",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("short_hash", sa.Text(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by_admin_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_admin_id"], ["admin_users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_affiliate_links_job_id"),
    )
    op.create_index(
        "ix_affiliate_links_short_hash", "affiliate_links", ["short_hash"], unique=True
    )
