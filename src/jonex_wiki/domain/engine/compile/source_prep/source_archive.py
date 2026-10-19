"""源侧留档：被体检摘出编译面的原文切片 + 判据，落 ``gen-<N>/source-archive/``。

**为什么必须留档**：体检的唯一有损动作是"这个单元不进编译面"。不留档就会立刻出现两个
问题——① 人工无法核对判据是否误杀（丢的是不是好内容）；② 后续接入图片处理/VLM 判官时，
没有"当时按什么判的"的底稿可以对照，也无法只对被摘除的部分重跑。

命名刻意避开引擎既有的 ``publication.quarantine_scope()`` / disposition ledger——那里的
quarantine 是**语义隔离**（claims 不进生成），这里的归档是**源侧原文留档**，同名不同义
只会让后来人误读。

落盘形态（与 ``raw/``、``source_artifacts.jsonl`` 平级，代次内自洽）::

    gen-1/source-archive/index.json     判据 + 计数 + 每条记录的元数据与切片哈希
    gen-1/source-archive/unit-0001.txt  该单元的**原文切片**（字节级原样，便于重放/复判）

``index.json`` 里的 ``provider``/``rule_version`` 让"谁判的、按哪版规则判的"随产物一起
固化：判据升级后，历史记录仍可解释。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jonex_wiki.domain.engine.compile.source_prep.inspect import (
    RULE_VERSION,
    InspectionReport,
)

ARCHIVE_SCHEMA = "jonex_wiki.source-archive.v1"
ARCHIVE_DIR_NAME = "source-archive"
INDEX_FILE = "index.json"


def archive_dir(gen_dir: Path) -> Path:
    """代次目录内的源侧留档目录（与 ``raw/`` 平级）。"""
    return Path(gen_dir) / ARCHIVE_DIR_NAME


def write_source_archive(gen_dir: Path, report: InspectionReport | None, text: str,
                         *, source_name: str = "") -> Path | None:
    """把体检结果落档；没有缺陷时**不建目录**（避免空目录噪声）。

    返回 ``index.json`` 路径；无记录返回 ``None``。
    """
    if report is None or report.is_empty:
        return None
    directory = archive_dir(gen_dir)
    directory.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    for position, defect in enumerate(report.defects):
        slice_text = text[defect.char_start:defect.char_end]
        name = f"unit-{position + 1:04d}.txt"
        (directory / name).write_text(slice_text, encoding="utf-8")
        record = defect.to_dict()
        record.update(file=name,
                      sha256=hashlib.sha256(slice_text.encode("utf-8")).hexdigest())
        records.append(record)

    index = {
        "schema": ARCHIVE_SCHEMA,
        "rule_version": RULE_VERSION,
        "source_name": source_name or report.source_name,
        "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "summary": report.summary(),
        "records": records,
    }
    path = directory / INDEX_FILE
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_source_archive(gen_dir: Path) -> dict | None:
    """读回留档（测试/运维用）：没有则返回 ``None``。"""
    path = archive_dir(gen_dir) / INDEX_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "ARCHIVE_DIR_NAME",
    "ARCHIVE_SCHEMA",
    "INDEX_FILE",
    "archive_dir",
    "read_source_archive",
    "write_source_archive",
]
