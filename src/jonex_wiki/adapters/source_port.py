"""SourceArtifactPort 适配器：只读解析期已写好的 ``source_artifacts.jsonl``。

解析期已完成归一化/结构打包/投影并落盘 JSONL;compile 侧不再从 raw 现场重建
ParseResult,而是逐行反序列化 ``SourceArtifact`` 后按 document_id → position
稳定排序返回。
"""
from __future__ import annotations

import json
import logging
from typing import List

from jonex_wiki.kernel.contracts import SourceArtifact
from jonex_wiki.kernel.ports import SourceArtifactPort
from jonex_wiki.kernel.errors import ValidationError
from jonex_wiki.storage.layout import Layout
from jonex_wiki.storage.repos import DocumentRepo

log = logging.getLogger(__name__)


class SourceArtifactPortAdapter(SourceArtifactPort):
    def __init__(self, session_factory, layout: Layout, repo: DocumentRepo | None = None,
                 *, settings=None, rows=None) -> None:
        self._session_factory = session_factory
        self._layout = layout
        self._repo = repo or DocumentRepo()
        self._rows = rows
        self.source_snapshot: tuple[tuple, ...] | None = None
        if settings is not None:
            from jonex_wiki.domain.parsing.identity import parse_input_signature
            self._parse_signature = parse_input_signature(settings)
        else:
            self._parse_signature = None

    async def get_artifacts(
        self,
        context_id: str,
        tenant_id: str,
        *,
        since_generation: int | None = None,
    ) -> List[SourceArtifact]:
        if since_generation is not None:
            # The compiler treats absent sources as deletions. Returning only
            # newer generations here would silently delete unchanged pages.
            raise ValueError("since_generation cannot filter a full compile inventory")
        # One port belongs to one compile attempt. Rechecks in the engine must
        # read that same inventory; the application validates its dependencies
        # against live rows under the publication lock.
        if self._rows is None:
            async with self._session_factory() as session:
                self._rows = await self._repo.list_parsed_for_compile(session, tenant_id, context_id)
        rows = self._rows
        self.source_snapshot = self.snapshot(rows)

        out: List[SourceArtifact] = []
        stale_documents: set[str] = set()
        for row in rows:
            path = self._layout.artifacts_path(
                tenant_id, context_id, row.document_id, row.content_generation
            )
            if not path.is_file():
                # 与 integration 一致:解析成功但 JSONL 缺失属数据损坏,必须暴露
                # 而不是静默跳过该文档。
                raise FileNotFoundError(
                    f"source_artifacts missing for doc={row.document_id} "
                    f"gen={row.content_generation}: {path}"
                )
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    artifact = SourceArtifact.model_validate(json.loads(line))
                    metadata = dict(artifact.source_metadata or {})
                    if (self._parse_signature is not None
                            and metadata.get("parse_input_signature") != self._parse_signature):
                        stale_documents.add(row.document_id)
                        continue
                    if row.source_name != metadata.get("source_name"):
                        old_name = metadata.get("source_name")
                        metadata["source_name"] = row.source_name
                        catalog = metadata.get("source_reading_materials")
                        if isinstance(catalog, dict):
                            catalog = json.loads(json.dumps(catalog, ensure_ascii=False))
                            identity = catalog.get("source_identity")
                            if isinstance(identity, dict):
                                identity["source_name"] = row.source_name
                            for unit in catalog.get("units", []):
                                if (isinstance(unit, dict)
                                        and unit.get("kind") in {"document", "workbook"}
                                        and unit.get("name") == old_name):
                                    unit["name"] = row.source_name
                            metadata["source_reading_materials"] = catalog
                    if row.recompile_generation:
                        metadata["recompile_generation"] = row.recompile_generation
                    artifact = artifact.model_copy(update={"source_metadata": metadata})
                    out.append(artifact)

        if stale_documents:
            docs = ", ".join(sorted(stale_documents))
            raise ValidationError(
                f"文档 {docs} 的解析规则已变化或旧产物缺少解析身份；"
                "请先重新解析这些文档，再编译知识库")

        # 稳定序:document_id → position(None 排最后,避免跨文档比较 None 崩溃)。
        out.sort(key=lambda a: (a.document_id, a.position if a.position is not None else 10**12))
        return out

    @staticmethod
    def snapshot(rows):
        return tuple(sorted(
            (row.document_id, row.content_generation, row.recompile_generation,
             row.source_name, row.source_sha256, row.parser_id, row.parser_version)
            for row in rows))

    @staticmethod
    def dependencies_current(snapshot, rows):
        current = {item[0]: item for item in SourceArtifactPortAdapter.snapshot(rows)}
        return all(current.get(item[0]) == tuple(item) for item in snapshot)
