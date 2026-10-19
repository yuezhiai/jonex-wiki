"""一次性回填工具:扫描旧布局下已有解析产物,列出需按新 raw 布局重解析的文档清单。

背景(方案 §12 / §8):raw-only 后解析产物形状变了——旧 ``gen-N/source_artifacts.jsonl``
新代码不吃(新代码要 ``gen-N/raw/`` 下的 content_list.json / source.txt + 带 schema 的
manifest.json),旧布局也不吃新产物,无法半回滚。切换后存量文档须按新 raw 布局全量重解析
(其中 MinerU 路由会真实花钱),本工具提供一次性扫描 + dry-run 清单。

**dry-run 语义(默认开启)**:只读扫描 + 输出清单与花销预估,不调 MinerU、不写任何产物、
不产生费用。``--no-dry-run`` 仅打印「将执行真实重解析」后退出(真实重解析走正常
parse 任务链路,由 API/executor 驱动,不在本工具内直连 MinerU)。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# 与 source_prep/raw_manifest.py 的 RAW_SCHEMA 保持一致(避免 import 引入 DB/引擎依赖)
RAW_SCHEMA = "jonex_wiki.raw_parse.v1"
RAW_MANIFEST_FILE = "manifest.json"

_GEN_RE = re.compile(r"^gen-(\d+)$")


@dataclass
class BackfillDoc:
    tenant_id: str
    kb_id: str
    document_id: str
    source_name: str
    route: str
    parser_id: str
    source_sha256: str
    latest_generation: int
    needs_mineru: bool
    estimated_pages: Optional[int] = None
    raw_block_count: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "kb_id": self.kb_id,
            "document_id": self.document_id,
            "source_name": self.source_name,
            "route": self.route,
            "parser_id": self.parser_id,
            "source_sha256": self.source_sha256,
            "latest_generation": self.latest_generation,
            "needs_mineru": self.needs_mineru,
            "estimated_pages": self.estimated_pages,
            "raw_block_count": self.raw_block_count,
        }


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _is_new_raw_layout(gen_dir: Path) -> bool:
    """gen-N/raw/manifest.json 存在且 schema 命中 → 已是新 raw 布局,无需重解析。"""
    raw_manifest = gen_dir / "raw" / RAW_MANIFEST_FILE
    if not raw_manifest.is_file():
        return False
    payload = _read_json(raw_manifest)
    return bool(payload and payload.get("schema") == RAW_SCHEMA)


_PAGE_RE = re.compile(rb"/Type\s*/Page[^s]")


def _estimate_pdf_pages(path: Path) -> Optional[int]:
    """轻量估 PDF 页数,无 PyPDF 依赖。

    页对象可能被压缩进 object stream(此时字面 ``/Type /Page`` 计数为 0),故依次尝试:
    1) 正则 ``/Type`` + 可选空白 + ``/Page``(含无空格写法)计数;
    2) Pages 树的 ``/Count N``(取最大 N);
    3) 都拿不到返回 None(上层用 raw_block_count 回退)。
    只做预算预估,不要求精确;非 PDF 或不可读返回 None。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(b"%PDF"):
        return None
    count = len(_PAGE_RE.findall(data))
    if count > 0:
        return count
    counts = [int(m) for m in re.findall(rb"/Count\s+(\d+)", data)]
    if counts:
        return max(counts)
    return None


def _latest_gen_dir(doc_dir: Path) -> Optional[tuple[int, Path]]:
    best: Optional[tuple[int, Path]] = None
    for child in doc_dir.iterdir() if doc_dir.is_dir() else ():
        m = _GEN_RE.match(child.name)
        if not m or not child.is_dir():
            continue
        gen = int(m.group(1))
        if best is None or gen > best[0]:
            best = (gen, child)
    return best


def _find_source_path(data_root: Path, tenant: str, kb: str, doc_id: str, source_name: str) -> Optional[Path]:
    """上传源文件路径:uploads/<doc-id>/<doc-id><ext>(旧布局与新布局同构)。"""
    uploads = data_root / tenant / kb / "uploads" / doc_id
    if not uploads.is_dir():
        return None
    for child in uploads.iterdir():
        if child.is_file():
            return child
    return None


def scan(data_root: Path) -> List[BackfillDoc]:
    """扫描旧布局,返回需重解析的文档清单(按 tenant/kb/doc 稳定排序)。"""
    result: List[BackfillDoc] = []
    parse_root = data_root
    # 遍历 <tenant>/<kb>/parse/<doc-id>/
    for tenant_dir in sorted(parse_root.iterdir()) if parse_root.is_dir() else ():
        if not tenant_dir.is_dir():
            continue
        for kb_dir in sorted(tenant_dir.iterdir()) if tenant_dir.is_dir() else ():
            parse_dir = kb_dir / "parse"
            if not parse_dir.is_dir():
                continue
            for doc_dir in sorted(parse_dir.iterdir()):
                if not doc_dir.is_dir():
                    continue
                latest = _latest_gen_dir(doc_dir)
                if latest is None:
                    continue
                gen, gen_dir = latest
                if _is_new_raw_layout(gen_dir):
                    continue  # 已是新 raw 布局
                old_manifest = _read_json(gen_dir / RAW_MANIFEST_FILE)
                if not old_manifest:
                    # 无 manifest 也视为需重解析(信息缺失,保守重解析)
                    old_manifest = {}
                route = str(old_manifest.get("route") or "mineru")
                source_name = str(old_manifest.get("source_name") or doc_dir.name)
                source_path = _find_source_path(parse_root, tenant_dir.name, kb_dir.name,
                                                doc_dir.name, source_name)
                pages = None
                if source_path is not None:
                    pages = _estimate_pdf_pages(source_path)
                result.append(BackfillDoc(
                    tenant_id=tenant_dir.name,
                    kb_id=kb_dir.name,
                    document_id=doc_dir.name,
                    source_name=source_name,
                    route=route,
                    parser_id=str(old_manifest.get("parser_id") or ""),
                    source_sha256=str(old_manifest.get("source_sha256") or ""),
                    latest_generation=gen,
                    needs_mineru=(route == "mineru"),
                    estimated_pages=pages,
                    raw_block_count=(old_manifest.get("raw_block_count")
                                     if isinstance(old_manifest.get("raw_block_count"), int) else None),
                ))
    return result


def render_text(docs: List[BackfillDoc], cost_per_page: float) -> str:
    mineru = [d for d in docs if d.needs_mineru]
    direct = [d for d in docs if not d.needs_mineru]
    # 页数优先,未知回退 raw_block_count(块数≈页数 proxy)
    def _units(d: BackfillDoc) -> int:
        if d.estimated_pages is not None:
            return d.estimated_pages
        return d.raw_block_count or 0
    total_units = sum(_units(d) for d in mineru)
    est_cost = total_units * cost_per_page
    lines: List[str] = []
    lines.append(f"待重解析文档:{len(docs)} 个(其中 MinerU {len(mineru)} 个 / direct {len(direct)} 个)")
    lines.append(f"MinerU 预估计价单位:{total_units}(页数优先,未知回退 raw_block_count;dry-run 未实际调用)")
    lines.append(f"预估 MinerU 花销:{est_cost:.2f} 元(@{cost_per_page} 元/单位)")
    lines.append("")
    for d in docs:
        if d.needs_mineru:
            units = f"{d.estimated_pages} 页" if d.estimated_pages is not None else f"{d.raw_block_count} 块(proxy)"
        else:
            units = "-"
        lines.append(
            f"  [{'MinerU' if d.needs_mineru else 'direct'}] {d.tenant_id}/{d.kb_id}/{d.document_id} "
            f"route={d.route} parser={d.parser_id or '-'} gen={d.latest_generation} "
            f"计价={units}  {d.source_name[:60]}"
        )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jonex-wiki-backfill",
        description="扫描旧解析布局,列出需按新 raw 布局重解析的文档(dry-run 默认,不调 MinerU)",
    )
    p.add_argument("--data-root", default=".results",
                   help="旧数据根目录(默认 .results)")
    p.add_argument("--cost-per-page", type=float, default=0.1,
                   help="MinerU 预估单价(元/页,默认 0.1)")
    p.add_argument("--json", action="store_true", help="以 JSON 输出清单")
    p.add_argument("--no-dry-run", action="store_true",
                   help="声明执行真实重解析(仅打印提示后退出;真实重解析走 parse 任务链路)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    data_root = Path(args.data_root).expanduser()
    if not data_root.is_dir():
        print(f"数据根目录不存在:{data_root}", file=sys.stderr)
        return 1
    docs = scan(data_root)
    if args.no_dry_run:
        print(f"dry-run 关闭:{len(docs)} 个文档将走正常 parse 任务链路重解析(本工具不直连 MinerU)。")
        print("请通过 API 上传/重解析入口触发,或按清单逐个提交 parse 任务。")
        return 0
    if args.json:
        print(json.dumps([d.to_dict() for d in docs], ensure_ascii=False, indent=2))
    else:
        print(render_text(docs, args.cost_per_page))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
