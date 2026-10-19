"""展示侧「同名归并」：一个实体一页（**读取时投影**，不改发布产物）。

背景
----
LLM 在编译期给同一实体生成了多个不同 slug，并且会在「实体目录」
（``artists/``）与每个「主题目录」（``lingnan*/``）下各建一份页面。
真实产物实测（3835 页 / 1143 个候选页，KB ``bec1ef09…``，revision 95404d13）：

* 同标题簇 **19** 个，涉及 66 页，归并后可减少 **47** 页；
* 其中 **跨目录 16 簇 / 同目录仅 3 簇** —— 所以归并键**不能**限制在同一目录，
  否则漏掉 84%（``陈树人`` 落在 artists / lingnan / lingnan-pai /
  lingnan-school / lingnan/painters 五个目录）。

归并键 = **归一标题**（不限制目录）。实测这 19 簇语义上都是同一实体；
同音不同人这类真正的同名歧义在本库未出现。误伤防线是 ``_EXCLUDED_PREFIXES``
——检索/证据侧页面（表格、源文档、证据片段、原始页）永不参与归并。

为什么在读取时投影，而不是在 ``build_bundle_files`` 改 corpus
------------------------------------------------------------
``build_query_corpus`` 产出的 corpus **同时喂检索树与阅读树**，在那一层归并会
改变检索产物。本模块只被 ``domain/reading``（展示链路）调用：发布 bundle
一字不动，随时可回滚，且不需要重新编译。

幂等性
------
``build_merge_map`` 是 (page_index, sizes) 的纯函数。对已归并的页面清单再跑一次，
每个标题簇只剩一个成员 → 无 redirect，得到恒等映射。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

# 检索/证据侧页面：永不参与归并（同标题也可能是不同来源意义上的页面）
_EXCLUDED_PREFIXES = ("tables/", "table-views/", "sources/", "source-evidence/", "raw/")

_WHITESPACE_RE = re.compile(r"[\s\u3000]+")
_INTERPUNCT_RE = re.compile(r"[·・\u00b7]")


def normalize_title(title: str) -> str:
    """标题归一：去掉所有空白与间隔号，再做 casefold。

    空白差异（``"关 山 月"``）与间隔号差异（``"阿·Q"``）在真实产物里都出现过，
    但它们指向同一实体，因此归一到同一个键。空标题返回空串——调用方据此跳过，
    避免把所有无标题页面并成一页。
    """
    text = (title or "").strip()
    text = _INTERPUNCT_RE.sub("", _WHITESPACE_RE.sub("", text))
    return text.casefold()


def is_merge_candidate(path: str) -> bool:
    """该路径是否参与同名归并（排除检索/证据侧目录）。"""
    return not path.startswith(_EXCLUDED_PREFIXES)


@dataclass(frozen=True)
class MergeMap:
    """展示侧归并结果：loser→winner 重定向 + 每个 winner 的全体成员。"""

    redirects: Mapping[str, str]
    members: Mapping[str, Tuple[str, ...]]
    titles: Mapping[str, str]

    @property
    def merged_count(self) -> int:
        """被并入别处的页面数（= 展示列表减少的条目数）。"""
        return len(self.redirects)

    def is_identity(self) -> bool:
        return not self.redirects

    def resolve(self, path: str) -> str:
        """把任意路径映射到展示用路径；非 loser 原样返回。"""
        return self.redirects.get(path, path)

    def cluster(self, path: str) -> Tuple[str, ...]:
        """返回该路径所属簇的全部成员（含 winner，已排序）；孤立页返回单元素。"""
        winner = self.resolve(path)
        return self.members.get(winner, (winner,))


_IDENTITY = MergeMap(redirects={}, members={}, titles={})


def build_merge_map(
    page_index: Mapping[str, Any],
    *,
    weight_of: Optional[Callable[[str], int]] = None,
    size_of: Optional[Callable[[str], int]] = None,
    is_candidate: Optional[Callable[[str], bool]] = None,
) -> MergeMap:
    """按归一标题聚类，给出 loser→winner 映射。

    选主顺序（确定性，保证同一输入永远同一 winner）：
    ① ``weight_of`` 更大（调用方传 claim 数——证据最多的那份是实体正页）；
    ② ``size_of`` 更大（正文体量）；
    ③ 路径层级更浅（``artists/x.md`` 优于 ``lingnan/painters/x.md``）；
    ④ 路径字典序更小。

    两个度量都要注入而不是写死：真实产物里「claim 最多」与「体量最大」在
    19 簇中有 3 簇给出不同 winner（``高奇峰``：artists 44 claims 但更厚，
    lingnan 60 claims 但更薄），所以主判据是 claim 数、体量只做 tie-break。
    """
    if is_candidate is None:
        is_candidate = is_merge_candidate

    buckets: Dict[str, list] = {}
    titles: Dict[str, str] = {}
    for path in page_index:
        path = str(path)
        if not is_candidate(path):
            continue
        entry = page_index[path]
        title = ""
        if isinstance(entry, dict):
            title = str(entry.get("title") or "")
        key = normalize_title(title)
        if not key:
            # 无标题页面不参与归并：否则会把一堆无关页面并成一页
            continue
        titles[path] = title
        buckets.setdefault(key, []).append(path)

    redirects: Dict[str, str] = {}
    members: Dict[str, Tuple[str, ...]] = {}
    for key, paths in buckets.items():
        if len(paths) < 2:
            continue
        ranked = sorted(
            paths,
            key=lambda p: (
                -(weight_of(p) if weight_of is not None else 0),
                -(size_of(p) if size_of is not None else 0),
                p.count("/"),
                p,
            ),
        )
        winner = ranked[0]
        members[winner] = tuple(sorted(paths))
        for loser in ranked[1:]:
            redirects[loser] = winner

    if not redirects:
        return _IDENTITY
    return MergeMap(redirects=redirects, members=members, titles=titles)


def remap_outline_paths(outline: Any, redirects: Mapping[str, str]) -> Any:
    """把目录（DB 里的 LLM 目录）中指向 loser 的条目重定向到 winner。

    结构无关的递归改写：任何含 ``path`` 键的字典都会被改写；任何一个
    「字典列表」内部按改写后的 path 去重（同一小节里两个 loser 并到同一 winner
    时只留一条）。主题/小节的层级与顺序一律原样保留。

    为什么在读取时做：目录落在 ``wiki_outlines`` 表里，是知识库级的，**不在
    bundle 内**，所以在编译产物里改不到它；而且读取时投影不必写库，等下一次
    「重新生成目录」基于归并后的页面清单起草完，重定向自然失效。
    """
    if not outline or not redirects:
        return outline

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            out = []
            seen = set()
            for item in node:
                mapped = walk(item)
                if isinstance(mapped, dict) and isinstance(mapped.get("path"), str):
                    path = mapped["path"]
                    if path in seen:
                        continue
                    seen.add(path)
                out.append(mapped)
            return out
        if isinstance(node, dict):
            result = {}
            for key, value in node.items():
                if key == "path" and isinstance(value, str):
                    result[key] = redirects.get(value, value)
                else:
                    result[key] = walk(value)
            return result
        return node

    return walk(outline)
