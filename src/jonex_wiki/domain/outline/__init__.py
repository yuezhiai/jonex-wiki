"""Outline（Wiki 阅读目录）domain：目录起草/调整/读取 + 纯函数清洗与投影。"""

from jonex_wiki.domain.outline.outline import (
    ADJUST_COVERAGE_FLOOR,
    MAX_PAGES_FOR_LLM,
    SOURCE_ADJUST,
    SOURCE_DRAFT,
    _clean_title,
    _catalog_snapshot,
    _covered_paths,
    _draft_max_tokens,
    _extract_json_object,
    _fingerprint,
    _outline_for_llm,
    build_topic_edges,
    sanitize_outline,
    scope_outline_to_pages,
)
from jonex_wiki.domain.outline.service import OutlineService

__all__ = [
    "ADJUST_COVERAGE_FLOOR",
    "MAX_PAGES_FOR_LLM",
    "SOURCE_ADJUST",
    "SOURCE_DRAFT",
    "OutlineService",
    "_catalog_snapshot",
    "_clean_title",
    "_covered_paths",
    "_draft_max_tokens",
    "_extract_json_object",
    "_fingerprint",
    "_outline_for_llm",
    "build_topic_edges",
    "sanitize_outline",
    "scope_outline_to_pages",
]
