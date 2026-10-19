"""create query_tasks

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "query_tasks",
        sa.Column("query_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(32), nullable=True),
        sa.Column("progress", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("bundle_sha", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_query_tasks_tenant_id", "query_tasks", ["tenant_id"])
    op.create_index("ix_query_tasks_kb_id", "query_tasks", ["kb_id"])


def downgrade() -> None:
    op.drop_index("ix_query_tasks_kb_id", table_name="query_tasks")
    op.drop_index("ix_query_tasks_tenant_id", table_name="query_tasks")
    op.drop_table("query_tasks")
