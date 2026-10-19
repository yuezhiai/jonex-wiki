"""add recompile_generation to documents

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("recompile_generation", sa.Integer(),
                                         nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("documents", "recompile_generation")
