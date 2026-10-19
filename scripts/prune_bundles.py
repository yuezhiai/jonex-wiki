#!/usr/bin/env python3
"""清理 jonex-wiki 引擎 compile/versions/ 下堆积的历史 bundle 版本目录。

保留策略（三者并集）：
1. ``published`` symlink 指向的当前版本（正在服务的）；
2. 最新的 ``--keep-history`` 个历史版本（rollback 用，默认 1）；
3. ``query_tasks`` 里 ``status in (queued, running)`` 且 ``bundle_sha`` 非空的
   行锁定的版本（查询任务仍在引用，不能删）。

安全护栏：数据库不可用时直接失败（避免误删查询引用版本）；存在 staging 任务 /
published 缺失或指向异常时跳过该 kb。

运行前提：jonex-wiki 服务已停（删除历史版本不碰 active，但 staging 判断与并发
编译/查询有竞争，停服最稳）。在项目根目录执行：

  # 预览（不删除）
  uv run python scripts/prune_bundles.py --data-root ./.results

  # 实际删除
  uv run python scripts/prune_bundles.py --data-root ./.results --apply

  # 保留最近 3 个历史版本
  ... --keep-history 3
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from jonex_wiki.infra.settings import Settings
from jonex_wiki.storage.db import db_connect_args
from jonex_wiki.storage.models import QueryTaskRow


async def collect_pins(engine) -> dict:
    """query_tasks 里 queued/running 且带 bundle_sha 的行 → {(tenant, kb): {sha}}。"""
    async with engine.connect() as conn:
        rows = (await conn.execute(
            select(
                QueryTaskRow.tenant_id,
                QueryTaskRow.kb_id,
                QueryTaskRow.bundle_sha,
            ).where(
                QueryTaskRow.status.in_(["queued", "running"]),
                QueryTaskRow.bundle_sha.isnot(None),
            )
        )).all()
    pins: dict = defaultdict(set)
    for tenant, kb, sha in rows:
        pins[(tenant, kb)].add(sha)
    return pins


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", default=None,
                    help="数据根目录（默认读 Settings.data_root）")
    ap.add_argument("--keep-history", type=int, default=1,
                    help="保留的历史版本数（默认 1）")
    ap.add_argument("--apply", action="store_true", help="实际删除（默认只预览）")
    args = ap.parse_args()

    settings = Settings()
    root = Path(args.data_root or settings.data_root).expanduser().resolve()
    if not root.is_dir():
        print(f"数据目录不存在：{root}", file=sys.stderr)
        return 1

    # 数据库不可用时直接失败，避免误删查询仍引用的版本。
    engine = create_async_engine(
        settings.database_url, connect_args=db_connect_args(settings))
    try:
        pins = await collect_pins(engine)
    finally:
        await engine.dispose()

    print(f"数据目录：{root}；模式：{'删除' if args.apply else '预览'}；"
          f"保留历史={args.keep_history}")
    deleted = 0
    skipped = 0

    for versions in sorted(root.glob("*/*/compile/versions")):
        if versions.is_symlink() or not versions.is_dir():
            continue
        compile_dir = versions.parent
        tenant = compile_dir.parent.parent.name
        kb = compile_dir.parent.name

        staging = compile_dir / "staging"
        if staging.is_dir() and any(staging.iterdir()):
            print(f"跳过 {tenant}/{kb}：存在 staging 任务")
            skipped += 1
            continue

        published = compile_dir / "published"
        if not published.is_symlink():
            print(f"跳过 {tenant}/{kb}：缺少 published 链接")
            skipped += 1
            continue
        active = published.resolve()
        if active.parent != versions.resolve() or not active.is_dir():
            print(f"跳过 {tenant}/{kb}：published 指向异常")
            skipped += 1
            continue

        history = sorted(
            (p for p in versions.iterdir()
             if p.is_dir() and not p.is_symlink() and p != active),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
            reverse=True,
        )
        keep = {active.name, *(p.name for p in history[:max(0, args.keep_history)])}
        keep.update(pins[(tenant, kb)])

        print(f"{tenant}/{kb}：当前={active.name[:12]}…；"
              f"保留={sorted(s[:12] for s in keep)}")
        for old in history:
            if old.name in keep:
                continue
            print(f"  {'删除' if args.apply else '拟删除'} {old.name[:12]}…")
            if args.apply:
                shutil.rmtree(old)
                deleted += 1

    print(f"完成：{'已删除' if args.apply else '拟清理清单已列出'}；"
          f"删除目录数={deleted}；跳过={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
