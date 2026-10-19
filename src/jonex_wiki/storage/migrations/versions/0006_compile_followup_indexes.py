"""Allow one queued follow-up while one compile is running.

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_tasks_running_compile", table_name="tasks")
    op.create_index(
        "uq_tasks_queued_compile", "tasks", ["tenant_id", "kb_id"], unique=True,
        postgresql_where=sa.text("kind = 'compile' AND status = 'queued'"),
        sqlite_where=sa.text("kind = 'compile' AND status = 'queued'"))


def downgrade() -> None:
    op.drop_index("uq_tasks_queued_compile", table_name="tasks")
    op.create_index(
        "uq_tasks_running_compile", "tasks", ["tenant_id", "kb_id"], unique=True,
        postgresql_where=sa.text("kind = 'compile' AND status IN ('queued', 'compiling')"),
        sqlite_where=sa.text("kind = 'compile' AND status IN ('queued', 'compiling')"))
