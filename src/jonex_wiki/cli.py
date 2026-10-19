"""Command-line client for the Jonex Wiki HTTP API.

The CLI is intentionally a thin HTTP client: it does not import the engine or
touch the database directly.  Start ``jonex-wiki-api`` first, then use this
command to upload, compile, query, and inspect a knowledge base.

Configuration is resolved in this order: command-line flag, environment
variable, built-in default.  The API key and tenant are sent as
``X-API-Key`` and ``X-Tenant-ID``.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx

from jonex_wiki import __version__

DEFAULT_BASE_URL = "http://127.0.0.1:8007"
DEFAULT_TENANT = "default"
ACTIVE_STATUSES = frozenset({"queued", "running", "parsing", "compiling"})
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


class CliError(RuntimeError):
    """A user-facing error; printed without a traceback."""


class ApiError(CliError):
    """An error returned by the HTTP API."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _add_common_args(parser: argparse.ArgumentParser, *, root: bool) -> None:
    """Add connection flags to the root parser or a subparser.

    Subparsers use ``SUPPRESS`` defaults so a flag placed before the subcommand
    (``jonex-wiki --tenant demo query ...``) is not overwritten by the subparser.
    """
    def default(value: Any) -> Any:
        return value if root else argparse.SUPPRESS

    parser.add_argument(
        "--base-url", default=default(os.environ.get("JONEX_WIKI_BASE_URL", DEFAULT_BASE_URL)),
        help=f"服务地址（默认 {DEFAULT_BASE_URL}；环境变量 JONEX_WIKI_BASE_URL）",
    )
    parser.add_argument(
        "--api-key", default=default(os.environ.get("JONEX_WIKI_API_KEY", "")),
        help="X-API-Key（默认读 JONEX_WIKI_API_KEY）",
    )
    parser.add_argument(
        "--tenant", default=default(os.environ.get("JONEX_WIKI_TENANT_ID", DEFAULT_TENANT)),
        help=f"X-Tenant-ID（默认 {DEFAULT_TENANT}；环境变量 JONEX_WIKI_TENANT_ID）",
    )
    parser.add_argument(
        "--http-timeout", type=float, default=default(60.0),
        help="单次 HTTP 请求超时秒数（默认 60）",
    )
    parser.add_argument(
        "--wait-timeout", type=float, default=default(1800.0),
        help="等待任务完成的超时秒数（默认 1800）",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=default(1.0),
        help="轮询任务状态的间隔秒数（默认 1）",
    )
    parser.add_argument(
        "--json", action="store_true", default=default(False),
        help="输出原始 JSON（适合脚本处理）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jonex-wiki",
        description="Jonex Wiki HTTP API 命令行客户端",
    )
    _add_common_args(parser, root=True)
    parser.add_argument("--version", action="version", version=f"jonex-wiki {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        _add_common_args(p, root=False)
        return p

    p = add("status", "检查服务健康状态")
    p.set_defaults(func=cmd_status)

    p = add("upload", "上传文件并触发解析")
    p.add_argument("file", help="本地文件路径")
    p.add_argument("--kb", required=True, help="知识库 ID")
    p.add_argument("--wait", action="store_true", help="等待解析任务结束")
    p.add_argument("--compile", action="store_true",
                   help="解析结束后继续等待自动触发的编译任务（隐含 --wait）")
    p.set_defaults(func=cmd_upload)

    p = add("compile", "触发或等待知识库编译")
    p.add_argument("--kb", required=True, help="知识库 ID")
    p.add_argument("--wait", action="store_true", help="等待编译任务结束")
    p.set_defaults(func=cmd_compile)

    p = add("query", "向已编译的知识库提问")
    p.add_argument("question", help="问题文本")
    p.add_argument("--kb", required=True, help="知识库 ID")
    p.add_argument("--thinking-mode", choices=("fast", "standard", "deep"), default="fast",
                   help="思考模式（默认 fast）")
    p.add_argument("--wait", dest="wait", action="store_true", default=True,
                   help="等待答案（默认）")
    p.add_argument("--no-wait", dest="wait", action="store_false",
                   help="只提交任务并打印 task_id")
    p.set_defaults(func=cmd_query)

    p = add("task", "查看任务状态")
    p.add_argument("task_id", help="task_id 或 qry-... 查询任务 ID")
    p.add_argument("--wait", action="store_true", help="等待任务结束")
    p.set_defaults(func=cmd_task)

    p = add("pages", "列出已发布 Wiki 的页面")
    p.add_argument("--kb", required=True, help="知识库 ID")
    p.set_defaults(func=cmd_pages)

    return parser


class Client:
    def __init__(self, base_url: str, api_key: str, tenant: str, timeout: float) -> None:
        if not api_key:
            raise CliError("缺少 API key：设置 JONEX_WIKI_API_KEY 或传 --api-key")
        if not tenant:
            raise CliError("缺少 tenant：设置 JONEX_WIKI_TENANT_ID 或传 --tenant")
        try:
            self.http = httpx.Client(
                base_url=base_url.rstrip("/"),
                headers={"X-API-Key": api_key, "X-Tenant-ID": tenant},
                timeout=httpx.Timeout(timeout, connect=10.0),
                # The CLI talks to a service the user names explicitly.  Ignore
                # ambient HTTP(S)_PROXY/NO_PROXY: a malformed NO_PROXY entry can
                # otherwise make even a localhost request fail before it starts.
                trust_env=False,
            )
        except (httpx.InvalidURL, ValueError) as exc:
            raise CliError(f"无效的服务地址 {base_url!r}：{exc}") from exc

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _error(self, response: httpx.Response) -> ApiError:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        code = None
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                code = err.get("code")
                message = err.get("message") or err.get("detail") or str(err)
                return ApiError(f"HTTP {response.status_code}: {message}", status=response.status_code,
                                code=code)
            message = payload.get("detail") or payload.get("message")
            if message:
                return ApiError(f"HTTP {response.status_code}: {message}", status=response.status_code)
        text = response.text.strip()
        return ApiError(f"HTTP {response.status_code}: {text[:500] or response.reason_phrase}",
                        status=response.status_code, code=code)

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CliError(f"无法连接服务 {self.http.base_url}：{exc}") from exc
        if response.status_code >= 400:
            raise self._error(response)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise CliError(f"服务返回了非 JSON 内容：{response.text[:300]}") from exc

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, payload: Optional[dict] = None, **kwargs: Any) -> Any:
        if payload is not None:
            kwargs["json"] = payload
        return self.request("POST", path, **kwargs)

    def upload(self, kb: str, path: Path) -> dict:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CliError(f"读取文件失败 {path}: {exc}") from exc
        return self.post(
            f"/api/v1/kbs/{quote(kb, safe='')}/documents",
            files={"file": (path.name, data, mime)},
        )

    def compile(self, kb: str) -> dict:
        return self.post(f"/api/v1/kbs/{quote(kb, safe='')}/compile")

    def query(self, kb: str, question: str, thinking_mode: str) -> dict:
        return self.post(
            f"/api/v1/kbs/{quote(kb, safe='')}/query",
            {"question": question, "thinking_mode": thinking_mode},
        )

    def task(self, task_id: str) -> dict:
        return self.get(f"/api/v1/tasks/{quote(task_id, safe='')}")

    def tasks(self, kb: str, kind: Optional[str] = None, limit: int = 20) -> dict:
        return self.get(f"/api/v1/kbs/{quote(kb, safe='')}/tasks", kind=kind, limit=limit)

    def pages(self, kb: str) -> dict:
        return self.get(f"/api/v1/kbs/{quote(kb, safe='')}/pages")


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _clip(text: Any, limit: int = 500) -> str:
    value = str(text or "").replace("\n", " ").strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "..."


def _print_citations(citations: list[dict]) -> None:
    if not citations:
        return
    print("\nCitations:")
    for index, citation in enumerate(citations, 1):
        handle = citation.get("handle") or f"[{index}]"
        location = (citation.get("page_path") or citation.get("path")
                    or citation.get("source_id") or "")
        print(f"  {handle} {location}".rstrip())
        excerpt = citation.get("excerpt")
        if excerpt:
            print(f"    {_clip(excerpt)}")


def _print_task(task: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(task, ensure_ascii=False, indent=2))
        return
    status = task.get("status", "?")
    kind = task.get("kind", "?")
    stage = task.get("stage")
    line = f"task={task.get('task_id', '?')} kind={kind} status={status}"
    if stage:
        line += f" stage={stage}"
    print(line)
    if task.get("error"):
        print(f"error: {_clip(task['error'])}")
    result = task.get("result")
    if isinstance(result, dict):
        answer = result.get("answer")
        if answer:
            print("\nAnswer:")
            print(answer)
        _print_citations(result.get("citations") or [])
    summary = task.get("summary")
    if summary and not result:
        print(f"summary: {_clip(json.dumps(summary, ensure_ascii=False), 400)}")


def _wait_task(client: Client, task_id: str, *, timeout: float, interval: float,
               quiet: bool) -> dict:
    deadline = time.monotonic() + timeout
    last: Optional[tuple] = None
    while True:
        task = client.task(task_id)
        current = (task.get("status"), task.get("stage"))
        if current != last and not quiet:
            stage = f" {task['stage']}" if task.get("stage") else ""
            _say(f"{task_id}: {task.get('status', '?')}{stage}")
            last = current
        if task.get("status") in TERMINAL_STATUSES:
            return task
        if time.monotonic() >= deadline:
            raise CliError(f"等待任务 {task_id} 超时（{timeout:g}s）；可用 `jonex-wiki task {task_id}` 继续查看")
        time.sleep(interval)


def _active_compile(client: Client, kb: str) -> Optional[dict]:
    tasks = client.tasks(kb, kind="compile", limit=20).get("tasks", [])
    for task in tasks:
        if task.get("status") in ACTIVE_STATUSES:
            return task
    return None


def _wait_compile(client: Client, kb: str, *, timeout: float, interval: float,
                  quiet: bool) -> Optional[dict]:
    """Return the compile task that belongs to the just-finished parse.

    The parse runner creates the follow-up compile task before marking the parse
    task done, so a single task-list read is enough.  If an older compile is the
    only one present, return it rather than starting a duplicate compile.
    """
    tasks = client.tasks(kb, kind="compile", limit=20).get("tasks", [])
    for task in tasks:
        if task.get("status") in ACTIVE_STATUSES:
            return _wait_task(client, task["task_id"], timeout=timeout,
                              interval=interval, quiet=quiet)
    if tasks:
        return client.task(tasks[0]["task_id"])
    return None


def cmd_status(client: Client, args: argparse.Namespace) -> int:
    health = client.get("/healthz")
    ready = client.get("/readyz")
    if args.json:
        print(json.dumps({"healthz": health, "readyz": ready}, ensure_ascii=False, indent=2))
    else:
        print(f"healthz: {health}")
        print(f"readyz:  {ready}")
    return 0


def cmd_upload(client: Client, args: argparse.Namespace) -> int:
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise CliError(f"文件不存在：{path}")
    response = client.upload(args.kb, path)
    if not (args.wait or args.compile):
        if args.json:
            print(json.dumps(response, ensure_ascii=False, indent=2))
        else:
            print(f"uploaded: document_id={response.get('document_id')} task_id={response.get('task_id')}")
        return 0
    if not args.json:
        print(f"uploaded: document_id={response.get('document_id')} task_id={response.get('task_id')}")

    task = _wait_task(client, response["task_id"], timeout=args.wait_timeout,
                      interval=args.poll_interval, quiet=args.json)
    if not args.json:
        _print_task(task, False)
    if task.get("status") != "done":
        if args.json:
            print(json.dumps(task, ensure_ascii=False, indent=2))
        return 1
    if not args.compile:
        if args.json:
            print(json.dumps(task, ensure_ascii=False, indent=2))
        return 0

    compile_task = _wait_compile(client, args.kb, timeout=args.wait_timeout,
                                 interval=args.poll_interval, quiet=args.json)
    if compile_task is None:
        _say("未发现自动触发的编译任务；可运行 `jonex-wiki compile --kb "
             f"{args.kb} --wait`")
        if args.json:
            print(json.dumps({"upload": response, "parse": task, "compile": None},
                             ensure_ascii=False, indent=2))
        return 0
    if args.json:
        print(json.dumps({"upload": response, "parse": task, "compile": compile_task},
                         ensure_ascii=False, indent=2))
    else:
        _print_task(compile_task, False)
    return 0 if compile_task.get("status") == "done" else 1


def cmd_compile(client: Client, args: argparse.Namespace) -> int:
    active = _active_compile(client, args.kb)
    if active is not None:
        _say(f"复用进行中的编译任务 {active['task_id']}")
        task = active
    else:
        try:
            task = client.compile(args.kb)
        except ApiError as exc:
            # A parse task may have enqueued compile between our list and POST.
            if exc.status != 409:
                raise
            active = _active_compile(client, args.kb)
            if active is None:
                raise
            _say(f"复用进行中的编译任务 {active['task_id']}")
            task = active
    if not args.wait:
        if args.json:
            print(json.dumps(task, ensure_ascii=False, indent=2))
        else:
            print(f"compile: task_id={task.get('task_id')} status={task.get('status', '?')}")
        return 0
    final = _wait_task(client, task["task_id"], timeout=args.wait_timeout,
                       interval=args.poll_interval, quiet=args.json)
    _print_task(final, args.json)
    return 0 if final.get("status") == "done" else 1


def cmd_query(client: Client, args: argparse.Namespace) -> int:
    response = client.query(args.kb, args.question, args.thinking_mode)
    if not args.wait:
        if args.json:
            print(json.dumps(response, ensure_ascii=False, indent=2))
        else:
            print(f"query: task_id={response.get('task_id')}")
        return 0
    task = _wait_task(client, response["task_id"], timeout=args.wait_timeout,
                      interval=args.poll_interval, quiet=args.json)
    _print_task(task, args.json)
    return 0 if task.get("status") == "done" else 1


def cmd_task(client: Client, args: argparse.Namespace) -> int:
    task = client.task(args.task_id)
    if args.wait:
        task = _wait_task(client, args.task_id, timeout=args.wait_timeout,
                          interval=args.poll_interval, quiet=args.json)
    _print_task(task, args.json)
    return 0 if task.get("status") in ("done", "running", "queued", "parsing", "compiling") else 1


def cmd_pages(client: Client, args: argparse.Namespace) -> int:
    data = client.pages(args.kb)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    pages = data.get("pages") or []
    published = data.get("published")
    suffix = f" (published={published})" if published is not None else ""
    print(f"pages: {len(pages)}{suffix}")
    for page in pages:
        path = page.get("path") or page.get("page_path") or page.get("id") or ""
        title = page.get("title") or page.get("name") or ""
        page_type = page.get("type") or ""
        suffix = f" [{page_type}]" if page_type else ""
        print(f"{path}\t{title}{suffix}".rstrip())
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        with Client(args.base_url, args.api_key, args.tenant, args.http_timeout) as client:
            return args.func(client, args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
