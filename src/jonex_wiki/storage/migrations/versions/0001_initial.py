"""initial tables + partial unique indexes

Revision ID: 0001
Revises:
Create Date: 2026-09-07
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 目标 schema 由 DB_SCHEMA 配置(search_path);不存在则创建(标识符安全转义)
    from jonex_wiki.infra.settings import Settings
    if op.get_bind().dialect.name == "postgresql":
        schema = Settings().db_schema
        quoted = schema.replace('"', '""')
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{quoted}"')
    op.create_table(
        "documents",
        sa.Column("document_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("route", sa.String(32), nullable=False),
        sa.Column("parser_id", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content_generation", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index("ix_documents_kb_id", "documents", ["kb_id"])
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("kb_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("document_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tasks_tenant_id", "tasks", ["tenant_id"])
    op.create_index("ix_tasks_kb_id", "tasks", ["kb_id"])
    op.create_table(
        "prompt_templates",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("kb_id", sa.String(64), primary_key=True),
        sa.Column("prompt_key", sa.String(32), primary_key=True),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # DB 级兜底:先查后插之间有 await,必须有部分唯一索引(审查第五节)
    op.create_index(
        "uq_tasks_running_compile", "tasks", ["tenant_id", "kb_id"], unique=True,
        postgresql_where=sa.text("kind = 'compile' AND status IN ('queued', 'compiling')"),
        sqlite_where=sa.text("kind = 'compile' AND status IN ('queued', 'compiling')"))
    op.create_index(
        "uq_documents_name_per_kb", "documents", ["tenant_id", "kb_id", "source_name"], unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"))


def downgrade() -> None:
    op.drop_table("prompt_templates")
    op.drop_table("tasks")
    op.drop_table("documents")
