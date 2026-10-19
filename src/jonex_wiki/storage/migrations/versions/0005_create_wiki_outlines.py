"""create wiki_outlines

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wiki_outlines",
        sa.Column("outline_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("outline_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("instruction", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("catalog_fingerprint", sa.String(64), nullable=False),
        sa.Column("outline", sa.JSON(), nullable=False),
        sa.Column("catalog_snapshot", sa.JSON(), nullable=False),
        sa.Column("edited_by", sa.String(128), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_wiki_outlines_tenant_id", "wiki_outlines", ["tenant_id"])
    op.create_index("ix_wiki_outlines_kb_id", "wiki_outlines", ["kb_id"])
    # partial unique：每 (tenant_id, kb_id) 单 active。双方言都必须写——
    # 漏 sqlite_where 会退化成全表 unique，插新 active 时旧 archived 占着
    # (tenant_id, kb_id) 直接 IntegrityError（SQLite 兜底路径目录功能不可用）。
    op.create_index(
        "uq_outlines_active_per_kb", "wiki_outlines", ["tenant_id", "kb_id"], unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"))


def downgrade() -> None:
    op.drop_index("uq_outlines_active_per_kb", table_name="wiki_outlines")
    op.drop_index("ix_wiki_outlines_kb_id", table_name="wiki_outlines")
    op.drop_index("ix_wiki_outlines_tenant_id", table_name="wiki_outlines")
    op.drop_table("wiki_outlines")
