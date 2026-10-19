"""MinerU self-hosted HTTP backend.

迁移自 core ``parsing/mineru/self_host.py``。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable

from jonex_wiki.domain.engine.compile.source_prep.blocks import ParseResult, sha256_file
from jonex_wiki.kernel.runtime_env import current_env
from .common import (
    OFFICE_SUFFIXES,
    MineruError,
    MineruOptions,
    normalize_inline_content_list,
)

logger = logging.getLogger(__name__)


class MineruSelfHostBackend:
    """Client for the lightweight ``mineru-api`` task contract."""

    parser_id = "mineru.self_host.v1"

    def __init__(self, *, base_url: str | None = None) -> None:
        self.base_url = (base_url or current_env().get("MINERU_SELFHOST_BASE_URL", "http://127.0.0.1:8000")).rstrip("/")
        self.backend = current_env().get("MINERU_SELFHOST_BACKEND", "pipeline") or "pipeline"
        self.default_lang = current_env().get("MINERU_SELFHOST_LANG", "ch") or "ch"
        self.poll_interval = _float_env("MINERU_SELFHOST_POLL_INTERVAL", 5.0)
        self.poll_timeout = _int_env("MINERU_SELFHOST_POLL_TIMEOUT", 1800)
        self.return_images = current_env().get("MINERU_SELFHOST_RETURN_IMAGES", "false").lower() in {"1", "true", "yes"}

    def parse(
        self,
        path: Path,
        *,
        output_dir: Path,
        source_id: str,
        document_id: str,
        options: MineruOptions | None = None,
        source_url: str | None = None,
    ) -> ParseResult:
        if not path.exists():
            raise FileNotFoundError(path)
        options = options or MineruOptions()
        output_dir.mkdir(parents=True, exist_ok=True)
        task_id = self._submit(path, options)
        self._poll(task_id, timeout=options.timeout)
        result = self._get(f"/tasks/{task_id}/result")
        results = result.get("results") or {}
        item = results.get(path.stem) if isinstance(results, dict) else None
        if not isinstance(item, dict):
            if isinstance(results, dict) and len(results) == 1:
                item = next(iter(results.values()))
            else:
                raise MineruError("ambiguous or missing self-host source result")
        raw = item.get("content_list")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise MineruError("MinerU self-host content_list 不是合法 JSON") from exc
        if not isinstance(raw, list):
            markdown = str(item.get("md_content") or "")
            raw = [{"type": "text", "text": markdown, "location_precision": "document"}] if markdown else []
        self._write_images(item.get("images"), output_dir)
        return normalize_inline_content_list(
            raw,
            output_dir=output_dir,
            source_name=path.name,
            parser_id=self.parser_id,
            source_sha256=sha256_file(path),
            source_id=source_id,
            document_id=document_id,
            markdown=str(item.get("md_content") or ""),
            source_url=source_url,
            packing=options.packing,
        )

    def check(self) -> bool:
        try:
            self._get("/health")
        except MineruError:
            return False
        return True

    def _submit(self, path: Path, options: MineruOptions) -> str:
        fields: Dict[str, Any] = {
            "backend": options.backend or self.backend,
            "return_content_list": True,
            "return_md": True,
            "return_images": self.return_images,
            "formula_enable": options.formula,
            "table_enable": options.table,
            "lang_list": [options.lang or self.default_lang],
            "parse_method": options.method,
        }
        # Office 全量（.doc/.docx/.ppt/.pptx/.xls/.xlsx）：统一携带
        # `convert_office_to_pdf=true`。不带它时 legacy 三件套会被自建 MinerU 直接 400
        # （"legacy Office 需要 convert_office_to_pdf=true"）。
        # 该字段是**放行**而非强制转换：实测带上后 .xls/.doc 的 converted_pdf_url 仍为空
        # = 服务端原生解析、没走 PDF 中转 ⇒ 对能原生解析的新格式是无副作用的 no-op。
        # ⚠️ 不要改用 profile=office-to-pdf（那才是强制转换）：实测它会静默产出空
        # content_list/md_content，且新格式加它同样变空。
        if path.suffix.lower() in OFFICE_SUFFIXES:
            fields["convert_office_to_pdf"] = True
        payload = self._multipart(path, fields)
        task_id = payload.get("task_id")
        if not task_id:
            raise MineruError("MinerU self-host 没有返回 task_id")
        return str(task_id)

    def _poll(self, task_id: str, *, timeout: int) -> None:
        deadline = time.monotonic() + int(timeout or self.poll_timeout)
        while time.monotonic() < deadline:
            status = self._get(f"/tasks/{task_id}")
            state = str(status.get("status") or "")
            if state == "completed":
                return
            if state == "failed":
                raise MineruError(f"MinerU self-host 解析失败：{status.get('error', 'unknown')}")
            time.sleep(self.poll_interval)
        raise MineruError(f"MinerU self-host 轮询超时：task_id={task_id}")

    def _get(self, path: str) -> Dict[str, Any]:
        request = urllib.request.Request(f"{self.base_url}{path}", headers={"Accept": "application/json"})
        try:
            from .common import retry_read
            def read_response():
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.read().decode("utf-8")
            raw = retry_read(read_response)
        except (OSError, urllib.error.URLError) as exc:
            raise MineruError(f"MinerU self-host GET {path} 失败") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MineruError(f"MinerU self-host GET {path} 返回非 JSON") from exc
        if not isinstance(payload, dict):
            raise MineruError(f"MinerU self-host GET {path} 返回结构无效")
        return payload

    def _multipart(self, path: Path, fields: Dict[str, Any]) -> Dict[str, Any]:
        boundary = "----JonexWiki" + hashlib.sha256(os.urandom(16)).hexdigest()[:24]
        line = b"\r\n"
        body = bytearray()
        for key, value in fields.items():
            values: Iterable[Any] = value if isinstance(value, (list, tuple)) else (value,)
            for item in values:
                body.extend(f"--{boundary}\r\n".encode())
                body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
                body.extend(("true" if item is True else "false" if item is False else str(item)).encode())
                body.extend(line)
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="files"; filename="{path.name}"\r\n'.encode())
        body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
        body.extend(path.read_bytes())
        body.extend(line)
        body.extend(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            f"{self.base_url}/tasks",
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                raw = response.read().decode("utf-8")
        except (OSError, urllib.error.URLError) as exc:
            raise MineruError("MinerU self-host 上传失败") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MineruError("MinerU self-host 上传响应不是 JSON") from exc
        if not isinstance(payload, dict):
            raise MineruError("MinerU self-host 上传响应结构无效")
        return payload

    @staticmethod
    def _write_images(images: Any, output_dir: Path) -> None:
        if not isinstance(images, dict):
            return
        image_dir = output_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        for name, encoded in images.items():
            if not isinstance(encoded, str):
                continue
            if encoded.startswith("data:") and "," in encoded:
                encoded = encoded.split(",", 1)[1]
            try:
                data = base64.b64decode(encoded)
            except (ValueError, TypeError):
                continue
            (image_dir / Path(str(name)).name).write_bytes(data)


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(current_env().get(name, str(default))))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.1, float(current_env().get(name, str(default))))
    except ValueError:
        return default
