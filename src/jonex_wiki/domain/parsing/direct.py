"""Deterministic format validation for the raw-only parse stage.

迁移自 core ``parsing/direct.py``。raw-only 下 parse 只负责「原样拿到」原文,
因此这里只保留文件读取/编码嗅探 ``_read_text``(替代 dev 的 ``decode_chain``);
结构打包(``pack_text`` / ``blocks_from_direct_text``)已迁到 compile 的
``source_prep/direct_ingest.py``,二进制容器冲突校验由
``source_prep/classify.validate_content`` 承担(替代 dev 的 ``sniff_binary_magic``)。
"""
from __future__ import annotations

from pathlib import Path

from jonex_wiki.kernel.runtime_env import current_env


def _read_text(path: Path, encoding: str | None = None) -> str:
    """bytes/path → text(编码链:BOM 探测 → 显式 env → utf-8-sig → gb18030),严格解码。

    与 core ``direct._read_text`` 一致:不设 latin-1 兜底,二进制/容器冲突 fail-closed。
    gb18030 兜底承接 integration 的 reading-repair 编码链(中文宽字符回退)。
    """
    data = path.read_bytes()
    selected = encoding or current_env().get("JONEX_WIKI_TEXT_ENCODING")
    if selected is None:
        selected = "utf-32" if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")) else (
            "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    try:
        text = data.decode(selected, errors="strict")
    except UnicodeDecodeError:
        if selected in ("gb18030", "utf-32", "utf-16"):
            raise
        text = data.decode("gb18030", errors="strict")
    lead = text[:4096].lstrip()
    if lead.startswith((r"{\rtf", "%PDF-")) or "\x00" in lead or data.startswith(b"PK\x03\x04"):
        raise ValueError(f"content conflicts with direct format {path.suffix}: {path}")
    return text


__all__ = ["_read_text"]
