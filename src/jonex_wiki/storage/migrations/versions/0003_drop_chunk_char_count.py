"""drop documents.chunk_count / char_count

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-11

回滚模型（方案 §10.2）：drop column 不可逆；回滚 = 整包回退上一 tag 后补一条
``add column + server_default=0 回填`` 的逆向 migration，不在此文件内自动逆转。
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("documents", "chunk_count")
    op.drop_column("documents", "char_count")


def downgrade() -> None:
    op.add_column("documents", sa.Column("chunk_count", sa.Integer(),
                                         nullable=False, server_default="0"))
    op.add_column("documents", sa.Column("char_count", sa.Integer(),
                                         nullable=False, server_default="0"))
