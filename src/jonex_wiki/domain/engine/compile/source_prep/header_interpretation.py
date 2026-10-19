"""Shared deterministic header interpretation for stored table grids.

One implementation serves both ends of the chain:

* the compile side, when a parsed table observation is persisted
  (``compile/source_prep/table_artifact.py``);
* the read side, when a frozen Bundle that predates this rule is opened and the
  stored interpretation can only be *read*, never rewritten
  (``query/table_reader.py``).

The module is pure: it takes the grid rows plus the source's merge ranges and
returns states, never touching sqlite or the filesystem.

Three questions are answered separately, because they need different evidence
and different callers may need only some of them:

``grid``
    Does a raw grid exist, and is it a complete rectangle?  This is what a raw
    coordinate read needs.
``header``
    Is the header (and the unit it declares) confirmed by the source's own
    merge geometry or by unambiguous grid text?  This is what addressing a
    value by field name needs.
delivery
    Whether *this request* was delivered completely is decided by the reader's
    paging layer, not here.

Only two multi-level structures are promoted, and both must have original
merge evidence:

1. a whole-width unit note whose next row carries the item and period labels
   (``| 单位：万元 |  |  |`` over ``| 项目 | 2023年 | 2022年 |``);
2. an upper period/group row over a lower field-title row, where the merge
   ranges state the subordination (``2023年`` spanning ``收入``/``成本``).

A row is never skipped just because it looks like a title, the first unique row
is never promoted to header, and the duplicate-label check is kept -- it is
evaluated on the *composed* field path, so a label that repeats under different
groups (``2023年 / % Change`` vs ``2024年 / % Change``) is confirmed while a
genuine duplicate stays unconfirmed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# The identity of this rule.  It travels with every interpretation written by
# the compile side, and a stored interpretation is only trusted - and compared
# field by field - when its marker equals this value.
HEADER_RULE_VERSION = "table-header-v3"

# A header is at most two levels: the supported structures are "one label row"
# and "group row over field-title row".  Deeper nesting is not guessed.
MAX_HEADER_ROWS = 2

_UNIT_NOTE = re.compile(
    r"^[（(]?\s*单位\s*[:：]\s*(?P<unit>[^（）()]+?)\s*[）)]?$")
# A trailing parenthesised qualifier ("（未经审计）") is not part of the unit.
_TRAILING_QUALIFIER = re.compile(r"(?<=\S)\s*[（(][^（）()]*[）)]$")
_UNIT_TOKEN = re.compile(
    r"^[（(]?\s*(?P<unit>(?:人民币)?[万亿千百]?元|美元|港元|欧元|日元|英镑|"
    r"百分点|%|‰|人|户|家|股|手|吨|千克|公斤|克|米|平方米|天|次|个|台|辆|"
    r"小时|分钟)\s*[）)]?$")
_PLACEHOLDER_LABEL = re.compile(r"column_[0-9]+\Z")


def parse_unit_note(text: str) -> Optional[str]:
    """The unit a whole-row note declares, or ``None`` when it declares none.

    Only an explicit ``单位：X`` note, or a cell that is nothing but a unit
    token, counts.  A cell that merely contains a unit inside a sentence is not
    a unit note: it would be a guess about which columns the sentence covers.
    """
    value = str(text or "").strip()
    if not value:
        return None
    match = _UNIT_NOTE.match(value) or _UNIT_NOTE.match(_TRAILING_QUALIFIER.sub("", value).strip())
    if match:
        return match.group("unit").strip()
    # Preserve the complete qualifier. Recognition of a unit row establishes
    # header geometry, not permission to apply its scale to every data row.
    english = value.strip("()（） ")
    if re.fullmatch(r"(?:dollars|USD|US dollars) in (?:millions|thousands|billions)"
                    r"(?:,? except per share (?:data|amounts))?", english, re.I):
        return english
    match = _UNIT_TOKEN.match(value)
    if match:
        return match.group("unit").strip()
    return None


def qualify_repeating_labels(labels: Sequence[str]) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """Qualify repeated sub-labels by their preceding unique group head.

    A real source often repeats a small set of sub-columns under each of
    several distinct group heads, e.g.::

        星期二 | 总数量 | 剩余数量 | 星期三 | 总数量 | 剩余数量 | ...

    Every repeated label is individually unusable: the reader refuses to
    resolve a repeated field name.  When the repetition is a perfectly regular
    block layout under unique heads, the label is in fact unambiguous once
    qualified by its head, so the columns stay addressable::

        星期二 | 星期二 / 总数量 | 星期二 / 剩余数量 | 星期三 | ...

    This stays deliberately narrow: it fires only for an exact repeat of one
    block shape with at least two groups, distinct non-placeholder heads, and a
    final label set that is unique.  Anything else keeps "report ambiguity,
    never guess" behaviour, and the caller keeps the duplicate as unconfirmed.

    Returns ``(labels, None)`` when the input is already unique or no regular
    layout is proven, otherwise the qualified labels plus a record of the rule
    that was applied.
    """
    labels = list(labels)
    total = len(labels)
    if len(set(labels)) == total:
        return labels, None
    for period in range(2, total // 2 + 1):
        for offset in range(0, total - 2 * period + 1):
            remainder = total - offset
            if remainder % period:
                continue
            groups = remainder // period
            if groups < 2:
                continue
            heads = [labels[offset + group * period] for group in range(groups)]
            if len(set(heads)) != groups:
                continue
            if any(_PLACEHOLDER_LABEL.match(head) for head in heads):
                continue
            pattern = labels[offset + 1:offset + period]
            if all(_PLACEHOLDER_LABEL.match(value) for value in pattern):
                continue
            if any(value in heads for value in pattern):
                # A "sub-label" that is the group head again states no
                # subordination: this is one label repeated, not a group over
                # distinct fields, and qualifying it would invent a hierarchy.
                continue
            if any(labels[offset + group * period + 1:offset + (group + 1) * period] != pattern
                   for group in range(groups)):
                continue
            prefix = labels[:offset]
            if len(set(prefix)) != len(prefix):
                continue
            qualified = list(prefix)
            for group in range(groups):
                qualified.append(heads[group])
                qualified.extend("{} / {}".format(heads[group], value) for value in pattern)
            if len(set(qualified)) != len(qualified):
                continue
            return qualified, {
                "rule": "repeating_group_header",
                "offset": offset,
                "period": period,
                "group_count": groups,
                "group_heads": heads,
                "sub_labels": pattern,
            }
    return labels, None


@dataclass(frozen=True)
class GridState:
    """Whether a raw grid exists and is a complete rectangle."""

    present: bool
    complete: bool
    reason: Optional[str]
    row_count: int
    column_count: int
    evidence: str = "grid_cells"

    def payload(self) -> Dict[str, Any]:
        return {"present": self.present, "complete": self.complete, "reason": self.reason,
                "row_count": self.row_count, "column_count": self.column_count,
                "evidence": self.evidence}


@dataclass(frozen=True)
class FieldSpec:
    """One column's field name, unit and the real header path behind it."""

    field_id: str
    column: int
    label: str
    path: Tuple[str, ...]
    header_row: int
    header_rows: Tuple[int, ...]
    unit: str
    label_source: str  # "header" | "merge_anchor" | "position"

    def payload(self) -> Dict[str, Any]:
        return {"field_id": self.field_id, "column": self.column, "label": self.label,
                "path": list(self.path), "header_row": self.header_row,
                "header_rows": list(self.header_rows), "unit": self.unit,
                "label_source": self.label_source}


@dataclass(frozen=True)
class HeaderState:
    """Whether the header and its unit are confirmed, and on what evidence."""

    confirmed: bool
    reason: Optional[str]
    structure: str
    header_rows: Tuple[int, ...]
    unit: str
    unit_row: Optional[int]
    unit_evidence: Optional[str]
    fields: Tuple[FieldSpec, ...]
    ambiguities: Tuple[str, ...]
    disambiguation: Optional[Mapping[str, Any]] = None

    def payload(self) -> Dict[str, Any]:
        return {"confirmed": self.confirmed, "reason": self.reason,
                "structure": self.structure, "header_rows": list(self.header_rows),
                "unit": self.unit, "unit_row": self.unit_row,
                "unit_evidence": self.unit_evidence,
                "ambiguities": list(self.ambiguities),
                "disambiguation": dict(self.disambiguation) if self.disambiguation else None,
                "fields": [field.payload() for field in self.fields]}


@dataclass(frozen=True)
class TableInterpretation:
    """The shared interpretation of one table region."""

    table_id: str
    rule_version: str
    grid: GridState
    header: HeaderState
    stored_interpretation_id: Optional[str] = None
    source: str = "compile"  # "compile" | "runtime"

    @property
    def structured_readable(self) -> bool:
        """Structured (name-addressed) reading needs both states confirmed."""
        return bool(self.grid.complete and self.header.confirmed)

    @property
    def status(self) -> str:
        return "accepted" if self.structured_readable else "ambiguous"

    def identity(self) -> str:
        """The interpretation identity bound into requests, cursors and caches.

        A compile-time interpretation keeps the id it was stored under; a
        read-time interpretation of a frozen Bundle gets its own deterministic
        id so a cursor issued under one rule can never be replayed under the
        other.  Nothing is written back.
        """
        if self.source == "compile" and self.stored_interpretation_id:
            return self.stored_interpretation_id
        return runtime_interpretation_id(self.table_id, self.stored_interpretation_id,
                                         self.rule_version)

    def payload(self) -> Dict[str, Any]:
        return {"header_rule": self.rule_version, "grid": self.grid.payload(),
                "header": self.header.payload()}


def interpretation_identity(table_id: str, rule_version: str = HEADER_RULE_VERSION) -> str:
    """The stored interpretation id for a table compiled under ``rule_version``."""
    return "interpretation-" + hashlib.sha256(
        "{}:{}".format(table_id, rule_version).encode("utf-8")).hexdigest()[:16]


def runtime_interpretation_id(table_id: str, stored_interpretation_id: Optional[str],
                              rule_version: str = HEADER_RULE_VERSION) -> str:
    """The read-time identity: same table, same stored reading, new rule."""
    return "interpretation-" + hashlib.sha256(
        "{}:{}:{}".format(table_id, stored_interpretation_id or "-", rule_version)
        .encode("utf-8")).hexdigest()[:16]


def _unconfirmed(table_id: str, rule_version: str, grid: GridState, reason: str,
                 structure: str = "unconfirmed", *,
                 stored_interpretation_id: Optional[str] = None,
                 source: str = "compile") -> TableInterpretation:
    return TableInterpretation(
        table_id=table_id, rule_version=rule_version, grid=grid,
        header=HeaderState(confirmed=False, reason=reason, structure=structure,
                           header_rows=(), unit="", unit_row=None, unit_evidence=None,
                           fields=(), ambiguities=(reason,)),
        stored_interpretation_id=stored_interpretation_id, source=source)


def interpret_table(rows: Sequence[Sequence[Any]], *, merges: Iterable[Sequence[int]] = (),
                    table_id: str = "", rule_version: str = HEADER_RULE_VERSION,
                    grid_complete: Optional[bool] = None,
                    grid_rows_total: Optional[int] = None,
                    stored_interpretation_id: Optional[str] = None,
                    source: str = "compile") -> TableInterpretation:
    """Interpret one table region from its grid and the source's merge ranges.

    ``rows`` are the region's own rows in reading order and ``merges`` are
    1-based inclusive ``(row_start, column_start, row_end, column_end)`` regions
    in that same local coordinate space.  A merge range is what makes a repeated
    text an inheritance instead of a value the source wrote twice; without it a
    covered position is simply missing.

    ``rows`` may be the header region only (a read-time caller must not scan a
    whole large table to name its columns); ``grid_rows_total`` then carries the
    real row count for reporting, and ``grid_complete`` the completeness the
    caller verified.  Both are reporting inputs only -- the interpretation
    itself is derived from the rows it was given.
    """
    grid_rows: List[List[str]] = [[str(value) for value in row] for row in rows]
    width = max((len(row) for row in grid_rows), default=0)
    present = bool(grid_rows) and width > 0
    complete = present and all(len(row) == width for row in grid_rows)
    if complete and grid_complete is False:
        complete = False
    if not complete:
        reason = "GRID_ABSENT" if not present else "GRID_INCOMPLETE"
        return _unconfirmed(
            table_id, rule_version,
            GridState(present=present, complete=False, reason=reason,
                      row_count=grid_rows_total if grid_rows_total is not None else len(grid_rows),
                      column_count=width),
            reason, "unconfirmed", stored_interpretation_id=stored_interpretation_id,
            source=source)

    grid = GridState(present=True, complete=True, reason=None,
                     row_count=grid_rows_total if grid_rows_total is not None else len(grid_rows),
                     column_count=width)

    regions: List[Tuple[int, int, int, int]] = []
    for merge in merges:
        try:
            r1, c1, r2, c2 = (int(value) for value in list(merge)[:4])
        except (TypeError, ValueError):
            continue
        if r1 < 1 or c1 < 1 or r2 < r1 or c2 < c1:
            continue
        if r2 > len(grid_rows) or c2 > width:
            # Geometry that does not fit the stored grid proves nothing here.
            continue
        regions.append((r1, c1, r2, c2))

    # A position covered by a merge range states no value of its own: whatever
    # the grid expansion repeated there belongs to the region's anchor.
    covered: Dict[Tuple[int, int], Tuple[int, int]] = {}
    for r1, c1, r2, c2 in regions:
        for row in range(r1, r2 + 1):
            for column in range(c1, c2 + 1):
                if (row, column) != (r1, c1):
                    covered[(row, column)] = (r1, c1)

    def own_text(row: int, column: int) -> Tuple[str, str]:
        anchor = covered.get((row, column))
        if anchor is not None:
            text = grid_rows[anchor[0] - 1][anchor[1] - 1].strip()
            return (text, "merge_anchor") if text else ("", "missing")
        text = grid_rows[row - 1][column - 1].strip()
        return (text, "header") if text else ("", "missing")

    structure_prefix = ""
    unit, unit_row, unit_evidence = "", None, None
    header_start = 1

    full_width = next((region for region in regions
                       if region[0] == 1 and region[2] == 1 and region[1] == 1
                       and region[3] == width), None)
    filled = [column for column in range(1, width + 1) if grid_rows[0][column - 1].strip()]
    if full_width is not None:
        token = parse_unit_note(grid_rows[0][0])
        if token is None:
            # One cell spanning the whole row is not a per-column header row,
            # and this rule does not guess that the real header is the next row.
            return _unconfirmed(
                table_id, rule_version, grid, "HEADER_FIRST_ROW_SPANNING_NON_UNIT",
                "spanning_caption", stored_interpretation_id=stored_interpretation_id,
                source=source)
        unit, unit_row, unit_evidence = token, 1, "full_width_merge"
        structure_prefix, header_start = "unit_note_", 2
    elif len(filled) == 1:
        token = parse_unit_note(grid_rows[0][filled[0] - 1])
        if token is not None:
            unit, unit_row, unit_evidence = token, 1, "single_filled_cell"
            structure_prefix, header_start = "unit_note_", 2

    if header_start > len(grid_rows):
        return _unconfirmed(table_id, rule_version, grid, "HEADER_ROW_MISSING",
                            structure_prefix + "unconfirmed",
                            stored_interpretation_id=stored_interpretation_id, source=source)

    # A row below the first header row is only a second header level when the
    # source says so: a merge range anchored in the first header row that spans
    # columns (grouping) or rows (a label covering both levels).  At least one
    # row must remain underneath for the second level to be a header, not data.
    grouping = [region for region in regions
                if region[0] == header_start and (region[3] > region[1] or region[2] > region[0])]
    depth = MAX_HEADER_ROWS if grouping and header_start + MAX_HEADER_ROWS <= len(grid_rows) else 1
    header_rows = tuple(range(header_start, header_start + depth))

    fields: List[FieldSpec] = []
    for column in range(1, width + 1):
        parts: List[str] = []
        sources: List[str] = []
        for row in header_rows:
            text, origin = own_text(row, column)
            if text and (not parts or parts[-1] != text):
                parts.append(text)
                sources.append(origin)
        label = " / ".join(parts) if parts else "column_{}".format(column)
        origin = "position" if not parts else (
            "merge_anchor" if "merge_anchor" in sources else "header")
        fields.append(FieldSpec(
            field_id="c{}".format(column), column=column, label=label,
            path=tuple(parts), header_row=header_rows[-1], header_rows=header_rows,
            unit=unit, label_source=origin))

    # An unnamed left stub is structurally addressable when the observed body
    # supplies textual row labels. Missing numeric-column headings stay unknown.
    body_stubs = [row[0].strip() for row in grid_rows[header_rows[-1]:] if row[0].strip()]
    if (width > 1 and fields[0].label_source == "position" and body_stubs
            and all(re.search(r"[A-Za-z\u3400-\u9fff]", text) for text in body_stubs)
            and all(field.label_source != "position" for field in fields[1:])):
        fields[0] = replace(fields[0], label="row_label", label_source="row_stub")
    # A conditional table unit is retained on the header, not asserted on each
    # column: e.g. millions EXCEPT per-share rows needs row-level scope checking.
    if re.search(r"\bexcept\b", unit, re.I):
        fields = [replace(field, unit="") for field in fields]
    ambiguities: List[str] = []
    if any(field.label_source == "position" for field in fields):
        ambiguities.append("HEADER_LABEL_MISSING")
    if not any(field.label_source != "position" for field in fields):
        ambiguities.append("HEADER_ROW_EMPTY")
    disambiguation: Optional[Dict[str, Any]] = None
    labels = [field.label for field in fields]
    if len(set(labels)) != len(labels):
        qualified, record = qualify_repeating_labels(labels)
        if record is not None:
            labels = qualified
            disambiguation = record
            fields = [replace(field, label=label) for field, label in zip(fields, labels)]
        else:
            ambiguities.append("HEADER_LABEL_DUPLICATE")

    confirmed = not ambiguities
    structure = structure_prefix + ("grouped_periods" if depth == MAX_HEADER_ROWS else "single_row")
    header = HeaderState(
        confirmed=confirmed,
        reason=None if confirmed else ";".join(dict.fromkeys(ambiguities)),
        structure=structure, header_rows=header_rows, unit=unit, unit_row=unit_row,
        unit_evidence=unit_evidence, fields=tuple(fields),
        ambiguities=tuple(dict.fromkeys(ambiguities)), disambiguation=disambiguation)
    return TableInterpretation(table_id=table_id, rule_version=rule_version, grid=grid,
                               header=header, stored_interpretation_id=stored_interpretation_id,
                               source=source)


__all__ = [
    "HEADER_RULE_VERSION",
    "MAX_HEADER_ROWS",
    "FieldSpec",
    "GridState",
    "HeaderState",
    "TableInterpretation",
    "interpret_table",
    "interpretation_identity",
    "parse_unit_note",
    "qualify_repeating_labels",
    "runtime_interpretation_id",
]
