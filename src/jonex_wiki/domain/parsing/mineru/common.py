"""Shared, provider-neutral MinerU helpers.

迁移自 core ``parsing/mineru/common.py``。raw-only 下额外提供
``read_raw_content_list``:只读取原始 content_list 数组(不做归一化/打包),
供 parse 落盘 ``content_list.json`` 使用。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import shutil
import socket
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from jonex_wiki.domain.engine.compile.source_prep.blocks import InputRoute, ParseBlock, blocks_from_items
from jonex_wiki.kernel.runtime_env import current_env

logger = logging.getLogger(__name__)

# Office 全量后缀（含 legacy OLE2）。自建 MinerU（yuexi-gpu :20490 fork）的 active
# 档位 `yuexi-default` 的 file_types **不含** `legacy_office`，legacy 三件套原样上传直接 400：
#   {"detail":"Unsupported file type: xls（legacy Office 需要 convert_office_to_pdf=true）"}
# 故 self_host 后端对整个 Office 家族都带 multipart 字段 `convert_office_to_pdf=true`；
# 该字段是**放行、不是强制转换**（实测 .xls / .doc 带上后 `converted_pdf_url` 仍为空 =
# 服务端**原生解析**、没走 PDF 中转），对能原生解析的新格式是无副作用的 no-op。
# local CLI 后端（`prepare_local_input`）则对同一集合自己用 LibreOffice 转 PDF。
# ⚠️ 不要改用档位 `profile=office-to-pdf`（那条路才是**强制转换**）：实测它 202 受理、
# completed、`converted_pdf_url` 有值、转出的 PDF 内容完好，但 `md_content` 与
# `content_list` **都是空的** —— 静默产出空结果，比 400 危险；新格式加它同样变空。
OFFICE_SUFFIXES = frozenset({".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"})


class MineruError(RuntimeError):
    """A parse failure with a provider-safe message."""


@dataclass(frozen=True)
class MineruOptions:
    method: str = "auto"
    lang: Optional[str] = None
    backend: Optional[str] = None
    formula: bool = True
    table: bool = True
    timeout: Optional[int] = None
    model_version: Optional[str] = None
    packing: Any = None


@dataclass(frozen=True)
class DownloadedInput:
    path: Path
    original_url: str
    final_url: str
    content_type: str


def run_mineru_cli(
    input_path: Path,
    output_dir: Path,
    options: MineruOptions,
) -> None:
    """Run the local MinerU CLI without leaking full subprocess output."""

    command = [
        "mineru",
        "-p",
        str(input_path),
        "-o",
        str(output_dir),
        "-m",
        options.method,
        "-b",
        options.backend or current_env().get("MINERU_LOCAL_BACKEND", "pipeline"),
    ]
    if options.lang:
        command.extend(["-l", options.lang])
    if not options.formula:
        command.extend(["-f", "false"])
    if not options.table:
        command.extend(["-t", "false"])
    logger.info("running MinerU CLI backend=%s input=%s", options.backend, input_path.name)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=options.timeout or int(current_env().get("MINERU_POLL_TIMEOUT", "1800")),
            check=False,
        )
    except FileNotFoundError as exc:
        raise MineruError(
            "本地 MinerU 命令不存在，请安装 mineru[core] 或改用 cloud/self_host 后端"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MineruError(f"MinerU CLI 超时（>{options.timeout}s）") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        safe_detail = detail[-1][:500] if detail else "unknown error"
        raise MineruError(f"MinerU CLI 失败（exit={completed.returncode}）：{safe_detail}")
    if completed.stderr:
        logger.debug("MinerU CLI stderr: %s", completed.stderr[-2000:])


def _unrepresented_warnings(blocks: List[ParseBlock]) -> List[str]:
    """原始 item 没能变成任何证据字段时的透出（**带计数与页码**）。

    与 ``source_inspection:*`` 同属"有损必须可见"的通道：这些 item 不进编译面，
    但不该静默消失。写清数量与页码，才能不看产物就判断丢了多少、丢在哪几页。
    """
    opaque: List[Dict[str, Any]] = []
    for block in blocks:
        opaque.extend(block.metadata.get("unrepresented_items") or [])
    if not opaque:
        return []
    pages = sorted({
        raw.get("page_idx") for raw in
        (item.get("raw_item") if isinstance(item, dict) else None for item in opaque)
        if isinstance(raw, dict) and isinstance(raw.get("page_idx"), int)})
    line = (f"source_unrepresented:items={len(opaque)} "
            "(raw items with no recognized evidence fields)")
    if pages:
        shown = ",".join(str(page + 1) for page in pages[:10])
        line += f":pages={shown}{',...' if len(pages) > 10 else ''}"
    return [line]


def _content_list_candidates(output_dir: Path, source_name: str) -> List[Path]:
    candidates = sorted(output_dir.rglob(f"{Path(source_name).stem}_content_list.json"))
    if not candidates:
        candidates = sorted(output_dir.rglob("*_content_list.json"))
    if not candidates:
        candidates = sorted(output_dir.rglob("content_list.json"))
    return candidates


def _markdown_candidates(output_dir: Path, source_name: str) -> List[Path]:
    candidates = sorted(output_dir.rglob(f"{Path(source_name).stem}.md"))
    if not candidates:
        candidates = sorted(output_dir.rglob("full.md"))
    if not candidates:
        candidates = sorted(output_dir.rglob("*.md"))
    return candidates


def read_raw_content_list(output_dir: Path, source_name: str) -> List[Dict[str, Any]]:
    """读取 MinerU 产出的原始 content_list 数组(raw-only,不做归一化)。

    兼容裸数组与 ``{"content": [...]}`` 两种形状;无 content_list 时回退到
    markdown 合成单条 text item(与 dev mineru 的 raw 采集语义一致)。
    """
    output_dir = output_dir.resolve()
    candidates = _content_list_candidates(output_dir, source_name)
    if len(candidates) > 1:
        raise MineruError("ambiguous MinerU content_list candidates")
    if candidates:
        try:
            payload = json.loads(candidates[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MineruError(f"无法读取 MinerU content_list: {candidates[0]}") from exc
        if isinstance(payload, dict):
            payload = payload.get("content")
        if isinstance(payload, list):
            return _flatten_content_items(payload)
        raise MineruError("MinerU content_list 必须是数组")

    markdown_candidates = _markdown_candidates(output_dir, source_name)
    if len(markdown_candidates) > 1:
        raise MineruError("ambiguous MinerU markdown candidates")
    if markdown_candidates:
        try:
            markdown = markdown_candidates[0].read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("cannot read MinerU markdown %s: %s", markdown_candidates[0], exc)
            markdown = ""
        if markdown:
            return [{"type": "text", "text": markdown, "location_precision": "document"}]
    raise MineruError(f"MinerU 没有产生 content_list 或 markdown：{output_dir}")


def normalize_output(
    output_dir: Path,
    *,
    source_name: str,
    parser_id: str,
    source_sha256: str,
    source_id: str,
    document_id: str,
    source_url: Optional[str] = None,
    packing: Any = None,
    inspection: Any = None,
    report: Any = None,
) -> "ParseResult":
    """Read MinerU's markdown/content-list output into a loss-aware result."""

    from jonex_wiki.domain.engine.compile.source_prep.blocks import ParseResult

    output_dir = output_dir.resolve()
    candidates = _content_list_candidates(output_dir, source_name)

    if len(candidates) > 1:
        raise MineruError("ambiguous MinerU content_list candidates")

    items: List[Dict[str, Any]] = []
    if candidates:
        try:
            payload = json.loads(candidates[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MineruError(f"无法读取 MinerU content_list: {candidates[0]}") from exc
        if isinstance(payload, dict):
            payload = payload.get("content")
        if not isinstance(payload, list):
            raise MineruError("MinerU content_list 必须是数组")
        items = [_safe_item(item, candidates[0].parent, output_dir)
                 for item in _flatten_content_items(payload)]

    markdown_candidates = _markdown_candidates(output_dir, source_name)
    if len(markdown_candidates) > 1:
        raise MineruError("ambiguous MinerU markdown candidates")
    markdown = ""
    if markdown_candidates:
        try:
            markdown = markdown_candidates[0].read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("cannot read MinerU markdown %s: %s", markdown_candidates[0], exc)

    if not items and markdown:
        items = [{"type": "text", "text": markdown, "location_precision": "document"}]
    if not items:
        raise MineruError(f"MinerU 没有产生 content_list 或 markdown：{output_dir}")

    blocks = blocks_from_items(
        items,
        source_name=source_name,
        parser_id=parser_id,
        route=InputRoute.MINERU,
        packing=packing,
        inspection=inspection,
        report=report,
    )
    return ParseResult(
        source_id=source_id,
        document_id=document_id,
        source_name=source_name,
        source_sha256=source_sha256,
        route=InputRoute.MINERU,
        parser_id=parser_id,
        parser_version="2.x+structure-pack-2",
        blocks=blocks,
        markdown=markdown,
        warnings=(report.warnings() if report is not None else [])
                 + _unrepresented_warnings(blocks)
                 + (["document-level location only; page coordinates unavailable"]
                    if blocks and all(block.page_number is None and not any(ref.get("page_number") is not None for ref in block.metadata.get("source_locations", [])) for block in blocks) else []),
        source_url=source_url,
        raw_output_dir=str(output_dir),
    )


def normalize_inline_content_list(
    items: Iterable[Dict[str, Any]],
    *,
    output_dir: Path,
    source_name: str,
    parser_id: str,
    source_sha256: str,
    source_id: str,
    document_id: str,
    markdown: str = "",
    source_url: Optional[str] = None,
    packing: Any = None,
    inspection: Any = None,
    report: Any = None,
) -> "ParseResult":
    """Normalize a self-host response that embeds content_list directly."""

    from jonex_wiki.domain.engine.compile.source_prep.blocks import ParseResult

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_items = _flatten_content_items(items)
    (output_dir / "content_list.json").write_text(json.dumps(raw_items, ensure_ascii=False), encoding="utf-8")
    if markdown:
        (output_dir / "full.md").write_text(markdown, encoding="utf-8")
    safe_items = [_safe_item(dict(item), output_dir, output_dir) for item in raw_items]
    blocks = blocks_from_items(
        safe_items,
        source_name=source_name,
        parser_id=parser_id,
        route=InputRoute.MINERU,
        packing=packing,
        inspection=inspection,
        report=report,
    )
    return ParseResult(
        source_id=source_id,
        document_id=document_id,
        source_name=source_name,
        source_sha256=source_sha256,
        route=InputRoute.MINERU,
        parser_id=parser_id,
        parser_version="2.x+structure-pack-2",
        blocks=blocks,
        markdown=markdown,
        warnings=(report.warnings() if report is not None else [])
                 + _unrepresented_warnings(blocks)
                 + (["document-level location only; page coordinates unavailable"]
                    if blocks and all(block.page_number is None and not any(ref.get("page_number") is not None for ref in block.metadata.get("source_locations", [])) for block in blocks) else []),
        source_url=source_url,
        raw_output_dir=str(output_dir),
    )


def _flatten_content_items(payload: Any) -> List[Dict[str, Any]]:
    """Flatten MinerU content-list nesting into one list of item dicts.

    MinerU-HTML 会把 items 包进一层额外数组(``[[{...}]]``);这里只沿 list 下钻、
    收集 dict,不进入 item 的字段,避免把 caption 等列表误当成顶层 items。
    """
    flat: List[Dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            flat.append(node)

    visit(payload)
    return flat


def _safe_item(item: Dict[str, Any], base_dir: Path, root_dir: Path) -> Dict[str, Any]:
    """Normalize aliases and resolve only assets inside the output root."""

    for old, new in (("img_caption", "image_caption"), ("img_footnote", "image_footnote")):
        if old in item and new not in item:
            item[new] = item[old]
        if new in item and old not in item:
            item[old] = item[new]
    for field in ("img_path", "table_img_path", "equation_img_path", "image_path"):
        value = item.get(field)
        if not isinstance(value, str) or not value:
            continue
        if value.startswith(("http://", "https://", "s3://")):
            continue
        candidate = (base_dir / value).resolve()
        if not candidate.is_relative_to(root_dir.resolve()):
            logger.warning("discarding unsafe MinerU asset path %s", value)
            item[field] = ""
        else:
            item[field] = candidate.relative_to(root_dir.resolve()).as_posix()
    return item


def extract_zip_safely(zip_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            root = destination.resolve()
            for member in archive.infolist():
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(root):
                    raise MineruError(f"MinerU 结果 zip 包含越界路径：{member.filename}")
            archive.extractall(destination)
    except zipfile.BadZipFile as exc:
        raise MineruError("MinerU 返回的结果不是有效 ZIP") from exc
    return destination


def download_url(url: str, destination: Path, *, timeout: int = 60, max_bytes: int = 50 * 1024 * 1024) -> DownloadedInput:
    """Download one URL into a bounded temporary file for MinerU.

    By default private/loopback destinations are rejected to prevent an input
    URL from turning the parser into an internal-network proxy.  Local test
    environments can explicitly set ``JONEX_WIKI_ALLOW_PRIVATE_URLS=1``.
    """

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        raise ValueError("URL 必须是无凭据的 http(s) 地址")
    if current_env().get("JONEX_WIKI_ALLOW_PRIVATE_URLS", "").lower() not in {"1", "true", "yes"}:
        _reject_private_host(parsed.hostname or "")

    destination.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Jonex-Wiki-MinerU/1.0", "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            final_parsed = urllib.parse.urlsplit(final_url)
            if current_env().get("JONEX_WIKI_ALLOW_PRIVATE_URLS", "").lower() not in {"1", "true", "yes"}:
                _reject_private_host(final_parsed.hostname or "")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
            suffix = Path(final_parsed.path).suffix.lower()
            if not suffix:
                suffix = mimetypes.guess_extension(content_type) or ".bin"
            target = destination / f"download{suffix}"
            total = 0
            with target.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"URL 内容超过 {max_bytes} 字节上限")
                    handle.write(chunk)
    except (OSError, urllib.error.URLError) as exc:
        raise MineruError(f"URL 下载失败：{type(exc).__name__}") from exc
    return DownloadedInput(target, url, final_url, content_type)


def _reject_private_host(host: str) -> None:
    import ipaddress

    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except OSError as exc:
        raise ValueError(f"无法解析 URL 主机：{host}") from exc
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            raise ValueError("URL 指向私有或保留网络地址；如确需本地测试请显式设置 JONEX_WIKI_ALLOW_PRIVATE_URLS=1")


def convert_office_to_pdf(path: Path, destination: Path) -> Path:
    """Convert legacy Office formats before handing them to MinerU CLI."""

    destination.mkdir(parents=True, exist_ok=True)
    command_name = shutil.which("libreoffice") or shutil.which("soffice")
    if not command_name:
        raise MineruError("本地 Office 解析需要 LibreOffice/soffice；云端或 self_host 可直接上传 Office")
    try:
        completed = subprocess.run(
            [command_name, "--headless", "--convert-to", "pdf", "--outdir", str(destination), str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MineruError("Office 转 PDF 超时") from exc
    if completed.returncode != 0:
        raise MineruError(f"Office 转 PDF 失败：{(completed.stderr or '').strip()[-500:]}")
    pdf = destination / f"{path.stem}.pdf"
    if not pdf.exists() or pdf.stat().st_size == 0:
        raise MineruError(f"Office 转 PDF 未产生有效文件：{path.name}")
    return pdf


def prepare_local_input(path: Path, staging_dir: Path) -> Path:
    if path.suffix.lower() in OFFICE_SUFFIXES:
        return convert_office_to_pdf(path, staging_dir / "office")
    if path.suffix.lower() in {".bmp", ".tif", ".tiff", ".gif", ".webp"}:
        try:
            from PIL import Image
        except ImportError as exc:
            raise MineruError("该图片格式需要 Pillow 转换为 PNG") from exc
        target = staging_dir / "images" / f"{path.stem}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as image:
            image.convert("RGB").save(target, "PNG")
        return target
    return path


def retry_read(operation):
    """Three total attempts for idempotent reads; never retry task submission."""
    import time
    import urllib.error
    attempts = int(current_env().get("MINERU_PARSE_MAX_RETRIES", "3"))
    backoff = float(current_env().get("MINERU_PARSE_RETRY_BACKOFF", "3"))
    if attempts < 1 or backoff < 0:
        raise ValueError("invalid MinerU retry configuration")
    for attempt in range(attempts):
        try:
            return operation()
        except (OSError, urllib.error.URLError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in {408, 429, 500, 502, 503, 504}:
                raise
            if attempt + 1 == attempts:
                raise
            time.sleep(backoff * (attempt + 1))
