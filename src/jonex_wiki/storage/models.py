import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from jonex_wiki.storage.db import Base


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DocumentRow(Base):
    __tablename__ = "documents"
    document_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _id("doc"))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    kb_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    route: Mapped[str] = mapped_column(String(32), nullable=False)      # text_direct/structured_direct/mineru
    parser_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="parsing", nullable=False)
    content_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0=从未解析成功
    recompile_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 单文档重编译计数(参与指纹)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("uq_tasks_queued_compile", "tenant_id", "kb_id", unique=True,
              postgresql_where=text("kind = 'compile' AND status = 'queued'"),
              sqlite_where=text("kind = 'compile' AND status = 'queued'")),
    )
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _id("task"))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    kb_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)          # parse/compile
    document_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class PromptTemplateRow(Base):
    __tablename__ = "prompt_templates"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True, nullable=False)
    kb_id: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)
    prompt_key: Mapped[str] = mapped_column(String(32), primary_key=True, nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class QueryTaskRow(Base):
    """独立查询任务表(§6.4):高频只读、生命周期不同,不复用 tasks。

    query_id 固定前缀 ``qry-``(GET /tasks/{id} 按前缀路由,杜绝跨类型撞 id)。
    """
    __tablename__ = "query_tasks"
    query_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _id("qry"))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    kb_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    bundle_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutlineRow(Base):
    """KB 级 Wiki 阅读目录留档行(目录功能下沉)。

    每次 draft/adjust 递增 outline_version,旧 active 转 archived(历史留档),
    新行 INSERT 为 active;partial unique index(WHERE status='active')保证每 KB
    单 active。source=draft/adjust 区分自动生成 vs 人工调整,是编译后自动触发
    是否覆盖人工成果的判断依据。
    """
    __tablename__ = "wiki_outlines"
    outline_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _id("outline"))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    kb_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    outline_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)        # draft / adjust
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    outline: Mapped[dict] = mapped_column(JSON, nullable=False)
    catalog_snapshot: Mapped[list] = mapped_column(JSON, nullable=False)
    edited_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
