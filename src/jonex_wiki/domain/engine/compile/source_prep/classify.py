"""Input classification and container/extension validation (compile side).

迁移自原实现 ``parsing/routes.py``。raw-only parse 在采集前用 ``classify_input`` 决定路由,
compile source_prep 复用同一套扩展名常量做 direct 结构打包。
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

from jonex_wiki.domain.engine.compile.source_prep.blocks import InputKind, InputRef, InputRoute

TEXT_EXTENSIONS = frozenset({".txt", ".md", ".markdown"})
STRUCTURED_EXTENSIONS = frozenset(
    {
        ".json",
        ".geojson",
        ".jsonl",
        ".ndjson",
        ".csv",
        ".tsv",
        ".xml",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".properties",
        ".log",
        ".rst",
    }
)
MINERU_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsm",
        ".png",
        ".jpg",
        ".jpeg",
        ".bmp",
        ".tif",
        ".tiff",
        ".gif",
        ".webp",
        ".html",
        ".htm",
        ".xhtml",
    }
)


def classify_input(value: str | Path | InputRef) -> Tuple[InputRef, InputRoute]:
    ref = value if isinstance(value, InputRef) else InputRef.from_value(value)
    if ref.kind is InputKind.URL:
        # URL 的实际后缀要等下载后才知道,但 URL 永远走 MinerU 路由。
        return ref, InputRoute.MINERU
    if ref.extension in TEXT_EXTENSIONS:
        return ref, InputRoute.TEXT_DIRECT
    if ref.extension in STRUCTURED_EXTENSIONS:
        return ref, InputRoute.STRUCTURED_DIRECT
    if ref.extension in {".xlsx", ".xlsm"}:
        return ref, InputRoute.SPREADSHEET_NATIVE
    if ref.extension in MINERU_EXTENSIONS:
        return ref, InputRoute.MINERU
    raise ValueError(
        f"Unsupported input format {ref.extension or '<no extension>'}. "
        "Use txt/md, JSON/JSONL/CSV/TSV/XML/YAML/TOML, or a MinerU-supported "
        "PDF/Office/image/HTML input."
    )


def validate_content(path: Path) -> None:
    """Check clear container/extension conflicts on the stored input copy."""
    with path.open("rb") as handle:
        head = handle.read(4096)
    suffix = path.suffix.lower()
    lead = head.lstrip()
    if lead.startswith(b"{\\rtf"):
        raise ValueError(f"RTF is not supported by the selected {suffix} route")
    if suffix == ".pdf" and b"%PDF-" not in head[:1024]:
        raise ValueError("PDF extension without a PDF header")
    if suffix in {".docx", ".pptx", ".xlsx", ".xlsm"} and not head.startswith(b"PK"):
        raise ValueError(f"{suffix} extension without a ZIP container")
    if suffix in TEXT_EXTENSIONS | STRUCTURED_EXTENSIONS and (
            head.startswith(b"PK\x03\x04") or lead.startswith(b"%PDF-")):
        raise ValueError(f"binary container conflicts with direct extension {suffix}")
