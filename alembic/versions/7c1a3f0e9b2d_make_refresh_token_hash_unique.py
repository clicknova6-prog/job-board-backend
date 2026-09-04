"""make refresh token hash unique

Revision ID: 7c1a3f0e9b2d
Revises: 3fbe4fe42552
Create Date: 2026-09-04 17:20:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c1a3f0e9b2d"
down_revision: str | Sequence[str] | None = "3fbe4fe42552"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("refresh_tokens_token_hash_idx", table_name="refresh_tokens")
    op.create_index(
        "refresh_tokens_token_hash_idx",
        "refresh_tokens",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("refresh_tokens_token_hash_idx", table_name="refresh_tokens")
    op.create_index(
        "refresh_tokens_token_hash_idx",
        "refresh_tokens",
        ["token_hash"],
        unique=False,
    )
