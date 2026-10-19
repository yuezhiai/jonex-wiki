"""单元级体检的执行器：判定 → 摘除 → 计数。

``inspect`` 是判据本体，本模块负责**把它接到打包前的单元列表上**——用与
``chunking.pack_text`` 完全相同的预算判据先过一遍，把注定装不下的单元摘出去，
再让打包逻辑只看剩下的单元。

**为什么用"前置筛查"而不是"改打包循环"**：``pack_text`` 是编译面唯一的结构守卫，
它的正文逻辑（表头重复、限定语吸附、按行分组、窗口切分）被大量测试覆盖。前置筛查
让这些逻辑**一行不改**，代价是要与它保持判据一致——所以：

- 判据**刻意保守**（宁可不筛，也不要筛错）：只处理"整个单元/某一行注定装不下"；
- 打包侧的 ``PackingError`` **原样保留**作为兜底：万一筛查漏判，行为回到"响亮失败"，
  不会出现"半隔离"的中间态；
- ``tests/test_source_inspection.py`` 用真实产物钉住"筛查过了就不再硬失败"。

被摘除的单元不丢：``source_archive.write_source_archive`` 把原文切片 + 判据一起落档。
"""
from __future__ import annotations

import re
from typing import Callable

from jonex_wiki.domain.engine.compile.source_prep import inspect as inspection_rules
from jonex_wiki.domain.engine.compile.source_prep.chunking import (
    WINDOW_EXEMPT_KINDS,
    PackingOptions,
    Unit,
    estimate_tokens,
    markdown_header_line_count,
    split_table_row,
)

TABLE_KINDS = frozenset({"table", "html_table"})
_ROW = re.compile(r"<tr\b[^>]*>.*?</tr\s*>", re.IGNORECASE | re.DOTALL)
_SAMPLE_LIMIT = 120


def _sample(value: str, limit: int = _SAMPLE_LIMIT) -> str:
    """原文片段（压空白后截断）：给人工抽查用，不参与判定。"""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _defect(text: str, unit: Unit, *, kind: str, detail: str, start: int | None = None,
            end: int | None = None, measured: int | None = None, budget: int | None = None,
            pointer: str = "", provider: str = inspection_rules.PROVIDER_HEURISTIC,
            sample_source: str | None = None) -> inspection_rules.SourceDefect:
    begin = unit.start if start is None else start
    finish = unit.end if end is None else end
    return inspection_rules.SourceDefect(
        kind=kind, unit_kind=unit.kind, char_start=begin, char_end=finish, detail=detail,
        measured=measured, budget=budget, pointer=pointer, provider=provider,
        sample=_sample(text[begin:finish] if sample_source is None else sample_source),
    )


def screen_units(text: str, units: list[Unit], config: PackingOptions,
                 policy: inspection_rules.InspectionPolicy,
                 report: inspection_rules.InspectionReport | None = None,
                 counter: Callable[[str], int] = estimate_tokens,
                 ) -> list[Unit]:
    """返回**可编译**的单元列表；注定装不下的单元记进 ``report`` 后摘除。

    没有任何缺陷时返回入参本身（同一个列表对象），保证"体检不改变正常路径"。
    """
    if not policy.enabled:
        return units
    report = report if report is not None else inspection_rules.InspectionReport()
    if not report.total_chars:
        report.total_chars = len(text)

    def fits(value: str, budget: int) -> bool:
        return len(value) <= config.max_chars and counter(value) <= budget

    kept: list[Unit] = []
    for index, unit in enumerate(units):
        value = text[unit.start:unit.end]

        # 结构超限：整单元注定装不下 → 摘除该单元（而不是让整份文档失败）。
        if unit.kind == "html_table" and not fits(value, config.table_target_tokens):
            defect = _oversized_html_table(text, unit, value, config, counter)
            if defect is not None:
                report.add(defect)
                continue
        elif unit.kind == "table" and not fits(value, config.table_target_tokens):
            defect = _oversized_markdown_table(text, unit, value, units, index, kept,
                                               config, counter)
            if defect is not None:
                report.add(defect)
                continue
        elif unit.kind in WINDOW_EXEMPT_KINDS and not fits(value, config.max_tokens):
            # record 等不参与窗口切分的单元：单条记录超上限时打包侧原本直接失败。
            report.add(_defect(text, unit, kind=inspection_rules.KIND_OVERSIZE_UNIT,
                               detail=f"complete {unit.kind} unit exceeds the package limit",
                               measured=counter(value), budget=config.max_tokens))
            continue

        kept.append(unit)

    return kept if len(kept) != len(units) else units


def _oversized_html_table(text: str, unit: Unit, value: str, config: PackingOptions,
                          counter: Callable[[str], int],
                          ) -> inspection_rules.SourceDefect | None:
    """HTML 表超预算时，判断是"能按行切"还是"整表该摘除"。

    判据与 ``chunking.pack_text`` 的 HTML 表分支逐条对齐：表头（首行 + 前后缀）
    装不下、或任意一行装不下，就等于打包侧必然抛 ``PackingError``。
    """
    rows = list(_ROW.finditer(value))
    if not rows:
        return _defect(text, unit, kind=inspection_rules.KIND_TABLE_NO_ROWS,
                       detail="HTML table has no complete rows")
    prefix_text = value[:rows[0].start()]
    suffix_text = value[rows[-1].end():]
    header = prefix_text + rows[0].group(0)
    header_tokens = counter(header + suffix_text)
    if header_tokens > config.row_max_tokens or len(header + suffix_text) > config.max_chars:
        return _defect(
            text, unit, kind=inspection_rules.KIND_OVERSIZE_TABLE_HEADER,
            detail=f"HTML table header exceeds the row budget ({header_tokens} est-tokens)",
            measured=header_tokens, budget=config.row_max_tokens, end=unit.start + rows[0].end())
    for match in rows[1:]:
        row = match.group(0)
        measured = counter(header + row + suffix_text)
        if measured > config.row_max_tokens or len(header + row + suffix_text) > config.max_chars:
            return _defect(
                text, unit, kind=inspection_rules.KIND_OVERSIZE_TABLE_ROW,
                detail=("HTML table row exceeds the row budget "
                        f"({measured} est-tokens / {len(row)} chars)"),
                measured=measured, budget=config.row_max_tokens,
                start=unit.start + match.start(), end=unit.start + match.end())
    return None


def _oversized_markdown_table(text: str, unit: Unit, value: str, units: list[Unit],
                              index: int, kept: list[Unit], config: PackingOptions,
                              counter: Callable[[str], int],
                              ) -> inspection_rules.SourceDefect | None:
    """Markdown 表超预算时同样判断能否按行切（含打包侧会吸附的限定语/脚注）。"""
    lines = value.splitlines(keepends=True)
    if not lines:
        return _defect(text, unit, kind=inspection_rules.KIND_TABLE_NO_ROWS,
                       detail="markdown table unit is empty")
    header_lines = markdown_header_line_count(lines)
    raw_header = "".join(lines[:header_lines])
    qualifiers = ""
    if kept:
        previous = kept[-1]
        candidate = text[previous.start:previous.end]
        if previous.kind == "text" and counter(candidate) <= config.context_tokens:
            qualifiers = candidate + ("\n" if not candidate.endswith("\n") else "")
    suffix = ""
    following_index = index + 1
    while following_index < len(units) and not text[units[following_index].start:units[following_index].end].strip():
        following_index += 1
    if following_index < len(units):
        candidate = text[units[following_index].start:units[following_index].end]
        if re.match(r"\s*(?:\[\^|注[:：]|备注[:：]|Note[:：]|\*\s)", candidate):
            suffix = candidate
    header = qualifiers + raw_header
    width = len(split_table_row(lines[0]))
    row_start = unit.start + len(raw_header)
    for row in lines[header_lines:]:
        if len(split_table_row(row)) != width:
            return _defect(text, unit, kind=inspection_rules.KIND_COLUMN_MISMATCH,
                           detail=("markdown table row has a different column count "
                                   f"than its header ({row_start})"),
                           start=row_start, end=row_start + len(row))
        measured = counter(header + row + suffix)
        if measured > config.row_max_tokens or len(header + row + suffix) > config.max_chars:
            return _defect(
                text, unit, kind=inspection_rules.KIND_OVERSIZE_TABLE_ROW,
                detail=("markdown table row exceeds the row budget "
                        f"({measured} est-tokens / {len(row)} chars)"),
                measured=measured, budget=config.row_max_tokens,
                start=row_start, end=row_start + len(row))
        row_start += len(row)
    measured_header = counter(header)
    if header and (measured_header > config.row_max_tokens or len(header) > config.max_chars):
        return _defect(text, unit, kind=inspection_rules.KIND_OVERSIZE_TABLE_HEADER,
                       detail=f"markdown table header exceeds the row budget ({measured_header})",
                       measured=measured_header, budget=config.row_max_tokens)
    return None


__all__ = ["screen_units"]
