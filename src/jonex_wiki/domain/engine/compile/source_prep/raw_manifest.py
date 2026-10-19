"""raw-only parse 落盘清单 schema(parse 侧写、compile source_prep 侧读)。

解析只负责「原样拿到」,归一化/打包/投影全部推迟到 compile;清单是两侧之间唯一共享的
元数据契约,不含任何块/投影产物(那些由 source_prep 现场重算)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

RAW_SCHEMA = "jonex_wiki.raw_parse.v1"

# raw 目录内文件名约定(parse 写、source_prep 读)
MANIFEST_FILE = "manifest.json"
CONTENT_LIST_FILE = "content_list.json"   # mineru 路由的原始 content_list 数组
SOURCE_TEXT_FILE = "source.txt"           # direct 路由的原文
PARSE_RESULT_FILE = "parse_result.json"   # spreadsheet 路由的完整 ParseResult(含 table_reference 块)


def build_manifest(*, document_id: str, source_name: str, source_sha256: str,
                   route: str, parser_id: str, parser_version: str,
                   content_generation: int) -> Dict[str, Any]:
    return {
        "schema": RAW_SCHEMA,
        "document_id": document_id,
        "source_name": source_name,
        "source_sha256": source_sha256,
        "route": route,
        "parser_id": parser_id,
        "parser_version": parser_version,
        "content_generation": content_generation,
    }


def write_manifest(path: Path, manifest: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_manifest(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != RAW_SCHEMA:
        raise ValueError(f"raw manifest schema mismatch: {payload.get('schema')!r}")
    return payload
