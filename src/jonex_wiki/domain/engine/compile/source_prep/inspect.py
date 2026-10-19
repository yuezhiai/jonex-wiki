"""编译输入体检（jonex_wiki 链路）：**只服务一条不变式——不摘掉打包器就会崩**。

``chunking.pack_text`` 的预算是编译面的结构守卫：装不下就 ``PackingError``。但超限只是
症状——一份 296 页文档里某个 OCR 退化的表格行有 6,933 字符，守卫把整份文档判死，而真正
该做的是把**那一个单元**挡在编译面之外、其余照常编译。

所以本模块只回答一件事：**哪个单元（或哪一行）注定装不下**。判定 → 处置（``chunking``
摘除该单元 + ``source_archive`` 原文留档 + ``warnings`` 透出）。

**边界（刻意收窄，别再往里加判据）**：

- 只收"结构超限"与它的两个前因（表没有行、行与表头列数不一致）；
- "内容质量"类问题（空表、复读、乱码……）**不进这里**：它们体积极小、不会让打包器崩，
  收进来只会多出一种"半失败态"。这类问题该由"产物形状契约"（见下）和后续 VLM 判官处理，
  而不是靠一个体积极小的单元被静默摘掉；
- 引擎不变式被破坏（嵌套表、非法 span、字符级装不下等**非单元级**问题）仍由
  ``PackingError`` 硬失败，不受本模块影响——筛查漏判时行为回到"响亮失败"，不会出现
  第三种结局。

**判据可插拔**：``provider`` 目前只有启发式 ``heuristic-v1``（纯文本统计，零成本）。
判定结果带 ``provider`` 与 ``rule_version``，落档后可回答"这条是谁、按哪版规则判的"。

**产物形状契约**（这次事故的正面教训）：``kb_type`` 透传丢了不会报错，只会让解析产物
从 ``content_list.json`` 静默退回 ``content.md``——产物形状变了却没人知道。所以 ingest
把**实际生效的 route / 产物名**回给平台（``contract``），平台在形状不符合预期时写进
``llm_wiki_compile_warnings``。断言，不是日志：正常就静默。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# 判据版本：判据集合改动必须升版本号，否则历史留档无法解释。
# v1 = 超限/空表/复读三类；v2 = 只留超限（空表与复读已删，见模块 docstring 的边界）。
RULE_VERSION = "source-inspect-v2"
PROVIDER_HEURISTIC = "heuristic-v1"

# ── 缺陷类型（kind）：全部对应"打包器会崩"，且都能定位到单元/行 ────────────
KIND_OVERSIZE_UNIT = "oversize_unit"                    # 整个单元超过整包上限
KIND_OVERSIZE_TABLE_ROW = "oversize_table_row"          # 表格某一行超出行上限
KIND_OVERSIZE_TABLE_HEADER = "oversize_table_header"    # 表头（含限定语）超出行上限
KIND_TABLE_NO_ROWS = "table_without_rows"               # 声明是表格却没有任何行
KIND_COLUMN_MISMATCH = "table_column_mismatch"          # 行与表头的列数不一致


@dataclass(frozen=True)
class SourceDefect:
    """一条可定位的源侧缺陷（判定结果；处置由调用方决定）。"""

    kind: str
    unit_kind: str
    char_start: int
    char_end: int
    detail: str
    provider: str = PROVIDER_HEURISTIC
    rule_version: str = RULE_VERSION
    measured: int | None = None      # 实测值（est-token）
    budget: int | None = None        # 命中的预算
    pointer: str = ""                # 块/表标识（如 prefix:table:{start}）
    page_number: int | None = None
    bbox: Any = None
    sample: str = ""                 # 原文片段（截断），供人工抽查

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "unit_kind": self.unit_kind,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "chars": self.chars,
            "detail": self.detail,
            "provider": self.provider,
            "rule_version": self.rule_version,
            "measured": self.measured,
            "budget": self.budget,
            "pointer": self.pointer,
            "page_number": self.page_number,
            "bbox": self.bbox,
            "sample": self.sample,
        }

    @property
    def chars(self) -> int:
        return max(0, self.char_end - self.char_start)


@dataclass
class InspectionReport:
    """一次体检的汇总：摘掉了什么 + 透出用的 warnings。

    同时是**留档与透出的唯一真相源**：``source_archive`` 写它的记录，``warnings()``
    给平台看（落到 ``llm_wiki_compile_warnings``），三者出自同一份数据。
    """

    source_name: str = ""
    total_chars: int = 0
    defects: list[SourceDefect] = field(default_factory=list)

    def add(self, defect: SourceDefect) -> None:
        self.defects.append(defect)

    @property
    def is_empty(self) -> bool:
        return not self.defects

    @property
    def isolated_chars(self) -> int:
        return sum(defect.chars for defect in self.defects)

    @property
    def isolated_ratio(self) -> float:
        return (self.isolated_chars / self.total_chars) if self.total_chars else 0.0

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for defect in self.defects:
            result[defect.kind] = result.get(defect.kind, 0) + 1
        return result

    def warnings(self, *, max_samples: int = 5) -> list[str]:
        """机器可读 + 人可读的 warnings（平台写 ``llm_wiki_compile_warnings``）。"""
        if self.is_empty:
            return []
        lines = [
            "source_inspection:{}:isolated={}:chars={}:ratio={:.4f}".format(
                RULE_VERSION, len(self.defects), self.isolated_chars, self.isolated_ratio),
            "source_inspection:kinds=" + ",".join(
                f"{kind}×{count}" for kind, count in sorted(self.counts().items())),
        ]
        for defect in self.defects[:max_samples]:
            lines.append(
                "source_inspection:isolated[{}] chars {}:{} {} ({})".format(
                    defect.kind, defect.char_start, defect.char_end, defect.detail,
                    defect.provider))
        if len(self.defects) > max_samples:
            lines.append(
                f"source_inspection:isolated_more={len(self.defects) - max_samples}"
                " (见 gen-N/source-archive/index.json)")
        return lines

    def summary(self) -> dict[str, Any]:
        return {
            "rule_version": RULE_VERSION,
            "defects": len(self.defects),
            "isolated_chars": self.isolated_chars,
            "total_chars": self.total_chars,
            "isolated_ratio": round(self.isolated_ratio, 6),
            "kinds": self.counts(),
        }


@dataclass(frozen=True)
class InspectionPolicy:
    """体检开关。

    关掉 = 完全回到"超限即 ``PackingError``"的旧语义（``JONEX_WIKI_INSPECT_ENABLED=off``）。
    这里**没有**比例闸门、也没有分档：摘除判据只有一条（会不会崩打包器），所以摘除比例
    不需要阈值兜底；真要"整体不可信就整份失败"，那是编译侧 ``max_passage_failure_rate``
    的职责，不在这里造第二个同义概念。
    """

    enabled: bool = True
    provider: str = PROVIDER_HEURISTIC

    @classmethod
    def disabled(cls) -> "InspectionPolicy":
        return cls(enabled=False)

    @classmethod
    def from_settings(cls, settings) -> "InspectionPolicy":
        """配置唯一权威是 Settings（与 ``PackingOptions.from_settings`` 同口径）。"""
        return cls(enabled=bool(getattr(settings, "inspect_enabled", True)))

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "InspectionPolicy":
        import os

        env = environment if environment is not None else os.environ
        enabled_raw = env.get("JONEX_WIKI_INSPECT_ENABLED")
        return cls(enabled=True if enabled_raw is None
                   else enabled_raw.strip().lower() in ("1", "true", "yes", "on"))


__all__ = [
    "InspectionPolicy",
    "InspectionReport",
    "KIND_COLUMN_MISMATCH",
    "KIND_OVERSIZE_TABLE_HEADER",
    "KIND_OVERSIZE_TABLE_ROW",
    "KIND_OVERSIZE_UNIT",
    "KIND_TABLE_NO_ROWS",
    "PROVIDER_HEURISTIC",
    "RULE_VERSION",
    "SourceDefect",
]
