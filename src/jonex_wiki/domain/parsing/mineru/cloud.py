"""MinerU v4 cloud API backend.

迁移自原实现 ``parsing/mineru/cloud.py``。为 raw-only 与取消检查点增加了:
- ``parse_raw``:只上传/轮询/下载/解压并返回原始 content_list 数组(不归一化),
  供 parse 落盘 ``content_list.json``;
- ``cancel_check``:轮询间隔之间调用(与 dev ``MineruClient`` 的取消检查点等价);
- ``poll_interval`` / ``poll_timeout``:可由 dev ``Settings`` 显式注入。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from jonex_wiki.domain.engine.compile.source_prep.blocks import ParseResult, sha256_file
from jonex_wiki.kernel.runtime_env import current_env
from .common import (
    MineruError,
    MineruOptions,
    extract_zip_safely,
    normalize_output,
    read_raw_content_list,
)

logger = logging.getLogger(__name__)


class MineruCloudBackend:
    """Upload one file to MinerU's Precision Extract API."""

    parser_id = "mineru.cloud.v1"

    def __init__(self, *, base_url: str | None = None, token: str | None = None,
                 poll_interval: float | None = None, poll_timeout: int | None = None,
                 model_version: str | None = None) -> None:
        self.base_url = (base_url or current_env().get("MINERU_API_BASE_URL", "https://mineru.net")).rstrip("/")
        self.token = (token or current_env().get("MINERU_API_TOKEN", "")).strip()
        self.model_version = (model_version or current_env().get("MINERU_MODEL_VERSION", "vlm")).strip() or "vlm"
        self.poll_interval = _float_env("MINERU_POLL_INTERVAL", 5.0) if poll_interval is None else float(poll_interval)
        self.poll_timeout = _int_env("MINERU_POLL_TIMEOUT", 1800) if poll_timeout is None else int(poll_timeout)

    def parse(
        self,
        path: Path,
        *,
        output_dir: Path,
        source_id: str,
        document_id: str,
        options: MineruOptions | None = None,
        source_url: str | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> ParseResult:
        if not path.exists():
            raise FileNotFoundError(path)
        options = options or MineruOptions()
        source_sha = sha256_file(path)
        output_dir.mkdir(parents=True, exist_ok=True)
        batch_id, upload_url = self._request_upload_url(path, options)
        self._upload(upload_url, path)
        result = self._poll(batch_id, timeout=options.timeout, cancel_check=cancel_check)
        extract_dir = self._download_result(result, output_dir)
        return normalize_output(
            extract_dir,
            source_name=path.name,
            parser_id=self.parser_id,
            source_sha256=source_sha,
            source_id=source_id,
            document_id=document_id,
            source_url=source_url,
            packing=options.packing,
        )

    def parse_raw(
        self,
        path: Path,
        *,
        output_dir: Path,
        source_id: str,
        document_id: str,
        options: MineruOptions | None = None,
        source_url: str | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> List[Dict[str, Any]]:
        """raw-only:上传/轮询/下载/解压后返回原始 content_list 数组(不归一化)。"""
        if not path.exists():
            raise FileNotFoundError(path)
        options = options or MineruOptions()
        output_dir.mkdir(parents=True, exist_ok=True)
        batch_id, upload_url = self._request_upload_url(path, options)
        self._upload(upload_url, path)
        result = self._poll(batch_id, timeout=options.timeout, cancel_check=cancel_check)
        extract_dir = self._download_result(result, output_dir)
        return read_raw_content_list(extract_dir, path.name)

    def check(self) -> bool:
        return bool(self.token)

    def _headers(self) -> Dict[str, str]:
        if not self.token:
            raise MineruError("使用 cloud 后端需要设置 MINERU_API_TOKEN")
        return {"Authorization": f"Bearer {self.token}", "Accept": "*/*"}

    def _json_request(self, method: str, path: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        body = None
        headers = self._headers()
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            from .common import retry_read
            def read_response():
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.read().decode("utf-8")
            raw = retry_read(read_response) if method == "GET" else read_response()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:500]
            raise MineruError(f"MinerU cloud {method} {path} HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise MineruError(f"MinerU cloud {method} {path} 网络失败") from exc
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MineruError(f"MinerU cloud {method} {path} 返回非 JSON") from exc
        if result.get("code") not in (0, "0", None):
            raise MineruError(f"MinerU cloud {method} {path} 返回错误：{result.get('msg', 'unknown')}")
        return result

    def _request_upload_url(self, path: Path, options: MineruOptions) -> Tuple[str, str]:
        file_payload: Dict[str, Any] = {
            "name": path.name,
            "data_id": hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:32],
        }
        if options.method == "ocr":
            file_payload["is_ocr"] = True
        payload: Dict[str, Any] = {
            "files": [file_payload],
            "model_version": "MinerU-HTML" if path.suffix.lower() in {".html", ".htm", ".xhtml"} else (options.model_version or self.model_version),
            "enable_formula": options.formula,
            "enable_table": options.table,
        }
        if options.lang:
            payload["language"] = options.lang
        result = self._json_request("POST", "/api/v4/file-urls/batch", payload)
        data = result.get("data") or {}
        batch_id = data.get("batch_id")
        urls = data.get("file_urls") or []
        if not batch_id or not urls:
            raise MineruError("MinerU cloud 没有返回上传 URL")
        return str(batch_id), str(urls[0])

    @staticmethod
    def _upload(upload_url: str, path: Path) -> None:
        parsed = urllib.parse.urlsplit(upload_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise MineruError("MinerU signed upload URL 无效")
        connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_cls(parsed.hostname, parsed.port, timeout=300)
        request_path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        try:
            data = path.read_bytes()
            connection.request("PUT", request_path, body=data, headers={"Content-Length": str(len(data))})
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="ignore")[:500]
        except (OSError, http.client.HTTPException) as exc:
            raise MineruError("MinerU cloud signed upload 失败") from exc
        finally:
            connection.close()
        if response.status not in {200, 201, 204}:
            raise MineruError(f"MinerU cloud signed upload HTTP {response.status}: {body}")

    def _poll(self, batch_id: str, *, timeout: int, cancel_check: Callable[[], None] | None = None) -> Dict[str, Any]:
        deadline = time.monotonic() + int(timeout or self.poll_timeout)
        last_state = ""
        while time.monotonic() < deadline:
            result = self._json_request("GET", f"/api/v4/extract-results/batch/{batch_id}")
            entries = (result.get("data") or {}).get("extract_result") or []
            if isinstance(entries, dict):
                entries = [entries]
            if entries:
                item = entries[0]
                state = str(item.get("state") or "")
                if state != last_state:
                    logger.info("MinerU cloud batch=%s state=%s", batch_id, state)
                    last_state = state
                if state == "done":
                    return item
                if state == "failed":
                    raise MineruError(f"MinerU cloud 解析失败：{item.get('err_msg', 'unknown')}")
            if cancel_check is not None:
                cancel_check()
            time.sleep(self.poll_interval)
        raise MineruError(f"MinerU cloud 轮询超时：batch_id={batch_id}")

    def _download_result(self, result: Dict[str, Any], output_dir: Path) -> Path:
        url = result.get("full_zip_url") or result.get("zip_url") or result.get("download_url")
        if not url:
            raise MineruError("MinerU cloud 结果没有 ZIP URL")
        zip_path = output_dir / "mineru_result.zip"
        extract_dir = output_dir / "mineru_result"
        request = urllib.request.Request(str(url), headers={"User-Agent": "Jonex-Wiki-MinerU/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=300) as response, zip_path.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        except (OSError, urllib.error.URLError) as exc:
            raise MineruError("MinerU cloud 结果下载失败") from exc
        return extract_zip_safely(zip_path, extract_dir)


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
