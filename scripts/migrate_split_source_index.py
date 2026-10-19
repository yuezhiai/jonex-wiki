#!/usr/bin/env python3
"""迁移脚本：把已编译库 navigation.json 内嵌的 source_records/source_locations
拆到独立的 query/source_index.json，并重算 bundle_sha + 切换 published 版本。

背景：navigation.json 里内嵌的 source_locations（大库实测 258MB）只被 query 检索
链路消费，reading 页面清单（wiki-contents / wiki/outline）完全用不到。拆分后
reading 侧 load_navigation 只读 ~10MB，接口从 15s+ 降到 <1s。

运行前提：jonex-wiki 服务已停（避免并发读写 published 目录；启动恢复会把
running 查询判 failed、queued 丢弃，历史行不再读旧 bundle_sha）。

用法：
  # 预览（默认只迁移每个 kb 的 published 版本）
  python scripts/migrate_split_source_index.py --data-root /data/jonex-wiki --dry-run

  # 执行（默认只迁移 published 版本）
  python scripts/migrate_split_source_index.py --data-root /data/jonex-wiki

  # 迁移全部历史版本目录
  python scripts/migrate_split_source_index.py --data-root /data/jonex-wiki --all-versions
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

_MANIFEST_EXCLUDED = "indexes/manifest.json"


def canonical_json(obj) -> str:
    """与 pipeline.canonical_json 同口径的确定性紧凑 JSON。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def bundle_sha_of(root: Path) -> str:
    """与 artifact_store._bundle_sha_of 同口径：除 indexes/manifest.json 外全部文件的聚合哈希。"""
    h = hashlib.sha256()
    rels = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.relative_to(root).as_posix() != _MANIFEST_EXCLUDED
    )
    for rel in rels:
        h.update(rel.encode())
        h.update(b"\0")
        h.update(hashlib.sha256((root / rel).read_bytes()).digest())
    return h.hexdigest()


def rebuild_manifest_files(root: Path) -> dict:
    """重建 manifest.files 清单：除 indexes/manifest.json 外所有文件 → sha256 hex。"""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.relative_to(root).as_posix() != _MANIFEST_EXCLUDED
    }


def migrate_version_dir(version_dir: Path, dry_run: bool) -> tuple[str, str] | None:
    """拆分一个版本目录；返回 (old_sha, new_sha)，无需迁移返回 None。

    dry_run 时 new_sha 为占位符（不实际写文件、不算真实哈希）。
    """
    nav_path = version_dir / "query" / "navigation.json"
    si_path = version_dir / "query" / "source_index.json"
    if not nav_path.is_file():
        return None
    if si_path.is_file():
        return None  # 已迁移
    nav = json.loads(nav_path.read_text("utf-8"))
    if "source_records" not in nav and "source_locations" not in nav:
        return None  # 新产物（已拆分）
    source_records = nav.pop("source_records", {})
    source_locations = nav.pop("source_locations", {})
    old_sha = version_dir.name

    print(
        f"  拆分 {old_sha[:12]}…  "
        f"(source_records={len(source_records)} 项, "
        f"source_locations={len(source_locations)} 项)"
    )
    if dry_run:
        return old_sha, "<new-sha>"

    si_path.parent.mkdir(parents=True, exist_ok=True)
    si_path.write_text(
        canonical_json({
            "schema_version": 2,
            "source_records": source_records,
            "source_locations": source_locations,
        }),
        encoding="utf-8",
    )
    nav_path.write_text(canonical_json(nav), encoding="utf-8")

    new_sha = bundle_sha_of(version_dir)
    manifest_path = version_dir / _MANIFEST_EXCLUDED
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["bundle_sha"] = new_sha
    manifest["files"] = rebuild_manifest_files(version_dir)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return old_sha, new_sha


def activate_version(compile_dir: Path, new_sha: str) -> None:
    """切换 published symlink → versions/{new_sha}（相对路径，与 layout.activate_version 一致）。"""
    link = compile_dir / "published"
    tmp_link = compile_dir / "published.tmp"
    if tmp_link.exists() or tmp_link.is_symlink():
        os.unlink(tmp_link)
    os.symlink(f"versions/{new_sha}", tmp_link)
    os.replace(tmp_link, link)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", default="/data/jonex-wiki")
    ap.add_argument("--all-versions", action="store_true",
                    help="迁移所有历史版本目录（默认只迁移 published 版本）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不实际改动")
    args = ap.parse_args()

    data_root = Path(args.data_root)
    if not data_root.is_dir():
        print(f"data-root 不存在: {data_root}", file=sys.stderr)
        return 1

    migrated = 0
    skipped = 0
    for tenant_dir in sorted(p for p in data_root.iterdir() if p.is_dir()):
        for kb_dir in sorted(p for p in tenant_dir.iterdir() if p.is_dir()):
            compile_dir = kb_dir / "compile"
            versions_dir = compile_dir / "versions"
            if not versions_dir.is_dir():
                continue

            published_link = compile_dir / "published"
            published_name = None
            if published_link.is_symlink():
                published_name = os.path.basename(os.readlink(published_link))

            if args.all_versions:
                target_dirs = sorted(d for d in versions_dir.iterdir() if d.is_dir())
            elif published_name and (versions_dir / published_name).is_dir():
                target_dirs = [versions_dir / published_name]
            else:
                target_dirs = []

            if not target_dirs:
                continue
            print(f"KB {kb_dir.name}:")

            for version_dir in target_dirs:
                old_sha = version_dir.name
                result = migrate_version_dir(version_dir, args.dry_run)
                if result is None:
                    skipped += 1
                    continue
                _, new_sha = result
                migrated += 1
                if args.dry_run:
                    continue

                new_dir = versions_dir / new_sha
                if new_dir.exists():
                    print(
                        f"  ⚠ 跳过重命名（new_sha 已存在）: {old_sha[:12]} → {new_sha[:12]}",
                        file=sys.stderr,
                    )
                    continue
                os.rename(version_dir, new_dir)
                if published_name == old_sha:
                    activate_version(compile_dir, new_sha)
                    print(f"  published symlink → versions/{new_sha[:12]}…")

    print(f"\n完成：迁移 {migrated} 个版本，跳过 {skipped} 个（已迁移/无需迁移）。"
          + ("（dry-run，未实际改动）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
