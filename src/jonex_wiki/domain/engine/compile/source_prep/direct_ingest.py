"""Direct-format structural packing (compile side).

raw-only parse 只落盘原文;这里按后缀做结构打包(pack_text),产出 ParseResult 供投影。
迁移自 core ``parsing/direct.py::parse_direct``,输入从「文件路径」改为「已读原文 + 后缀」——
文件读取/编码嗅探/sha256 属 parse 侧职责,已在 raw 落盘阶段完成。
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Optional

from jonex_wiki.domain.engine.compile.source_prep.blocks import InputRoute, ParseResult
from jonex_wiki.domain.engine.compile.source_prep.chunking import PackingOptions, Unit, pack_text
from jonex_wiki.domain.engine.compile.source_prep.classify import STRUCTURED_EXTENSIONS, TEXT_EXTENSIONS


def blocks_from_direct_text(
    text: str,
    *,
    suffix: str,
    source_name: str,
    source_id: str,
    document_id: str,
    source_sha256: str,
    parser_id: str,
    parser_version: str,
    packing: Optional[PackingOptions] = None,
    csv_has_header: bool = True,
    inspection=None,
    report=None,
) -> ParseResult:
    """按后缀做结构打包;结构化格式(json/jsonl/csv/tsv)按记录/行切,不跨记录合并。

    ``inspection``/``report`` 见 ``chunking.pack_text``:开启体检时,注定装不下的
    单元(如 OCR 退化的超大表格行)被摘出编译面并记进 ``report``,其余照常打包;
    不传则完全是既有语义。
    """
    extension = suffix.lower()
    if extension not in TEXT_EXTENSIONS | STRUCTURED_EXTENSIONS:
        raise ValueError(f"unsupported direct format: {extension}")
    raw = text
    units: Optional[list[Unit]] = None
    extra: dict[str, Any] = {}
    if extension in {".json", ".geojson"}:
        parsed = json.loads(raw)
        extra = {"value_type": type(parsed).__name__}
        decoder = json.JSONDecoder()
        if isinstance(parsed, (list, dict)):
            units, start, cursor = [], 0, len(raw) - len(raw.lstrip()) + 1
            while cursor < len(raw):
                while cursor < len(raw) and (raw[cursor].isspace() or raw[cursor] == ","):
                    cursor += 1
                if cursor >= len(raw) or raw[cursor] in "]}":
                    break
                _, end = decoder.raw_decode(raw, cursor)
                if isinstance(parsed, dict):
                    cursor = end
                    while raw[cursor].isspace() or raw[cursor] == ":":
                        cursor += 1
                    _, end = decoder.raw_decode(raw, cursor)
                units.append(Unit(start, end, "record"))
                start = cursor = end
            if units:
                units[-1] = Unit(units[-1].start, len(raw), "record")
            else:
                units = [Unit(0, len(raw), "record")] if raw else []
    elif extension in {".jsonl", ".ndjson"}:
        units, offset, start, records = [], 0, 0, []
        for line_no, line in enumerate(raw.splitlines(keepends=True), 1):
            offset += len(line)
            if line.strip():
                json.loads(line)
                units.append(Unit(start, offset, "record"))
                records.append({"line": line_no, "char_start": start, "char_end": offset})
                start = offset
        if units:
            units[-1] = Unit(units[-1].start, len(raw), "record")
        elif raw:
            units = [Unit(0, len(raw), "record")]
        extra = {"records": records}
    elif extension in {".csv", ".tsv"}:
        delimiter = "\t" if extension == ".tsv" else ","
        reader = csv.reader(io.StringIO(raw, newline=""), delimiter=delimiter, strict=True)
        rows: list[list[str]] = []
        raw_ends: list[int] = []
        source_lines = raw.splitlines(keepends=True)
        for row in reader:
            rows.append(row)
            raw_ends.append(sum(map(len, source_lines[: reader.line_num])))

        def serialize(row):
            return ("| " + " | ".join(
                cell.replace("\\", "\\\\").replace("|", r"\|")
                .replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")
                for cell in row) + " |\n")

        if rows:
            header = rows[0] if csv_has_header else [f"column_{i + 1}" for i in range(max(map(len, rows)))]
            data = rows[1:] if csv_has_header else rows
            canonical = serialize(header) + serialize(["---"] * len(header)) + "".join(map(serialize, data))
            blocks = pack_text(canonical, prefix=Path(source_name).stem, options=packing,
                               inspection=inspection, report=report)
            cursor = 0
            for block in blocks:
                row_range = block.metadata.get("row_range", [0, len(data)])
                end_record = row_range[1] + int(csv_has_header)
                end = raw_ends[end_record - 1] if end_record else 0
                if block is blocks[-1]:
                    end = len(raw)
                block.metadata.update(
                    snapshot_text=raw[cursor:end], raw_char_start=cursor,
                    raw_char_end=end, csv_has_header=csv_has_header, format=extension[1:])
                cursor = end
        else:
            blocks = pack_text(raw, prefix=Path(source_name).stem, options=packing,
                           inspection=inspection, report=report)
        return _result(source_name, source_id, document_id, source_sha256,
                       parser_id, parser_version, extension, raw, blocks, report=report)
    blocks = pack_text(raw, prefix=Path(source_name).stem, options=packing, units=units,
                       inspection=inspection, report=report)
    for block in blocks:
        block.metadata.update(format=extension[1:])
        if "records" in extra:
            block.metadata["records"] = [r for r in extra["records"]
                                         if r["char_end"] > block.metadata["char_start"]
                                         and r["char_start"] < block.metadata["char_end"]]
        else:
            block.metadata.update(extra)
    return _result(source_name, source_id, document_id, source_sha256,
                   parser_id, parser_version, extension, raw, blocks, report=report)


def _result(source_name, source_id, document_id, source_sha256,
            parser_id, parser_version, extension, raw, blocks, report=None) -> ParseResult:
    route = (InputRoute.TEXT_DIRECT if extension in TEXT_EXTENSIONS
             else InputRoute.STRUCTURED_DIRECT)
    return ParseResult(
        source_id=source_id, document_id=document_id, source_name=source_name,
        source_sha256=source_sha256, route=route, parser_id=parser_id,
        parser_version=parser_version, blocks=blocks, markdown=raw,
        # 体检结论随 ParseResult 走既有透出通道(to_dict/artifacts metadata),
        # 平台据此写 llm_wiki_compile_warnings;原文切片由 source_archive 留档。
        warnings=(report.warnings() if report is not None else []))
