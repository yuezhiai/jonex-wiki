"""Pure builders for table cell / table note citation records.

Ported from integration ``storage/directory_bundle.py``.  In the earlier integration the
engine may not import the shell (``adapters/`` / ``storage/``), so the two
builders live here instead: they are pure functions over a reference plus the
cell (or note) the Reader returned, and they are the single place where a
table citation's ``claim_id``, ``excerpt`` and value hash are derived.  One
builder for registration and for cold re-open means the hash and the excerpt
can never drift apart.  No model call, no date formatting, no formula
evaluation.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping

TABLE_CELL_KIND = "table_cell"
# A table's own caption/footnote is source text too.  When a read delivers one
# and the relation check uses it, it must be able to become evidence with a
# handle, or the final answer can never state the condition it was shown.
TABLE_NOTE_KIND = "table_note"
# The snapshot keys are fixed; a missing value stays null, never "" or 0.
CELL_SNAPSHOT_KEYS = ("raw_type", "raw_value", "formula", "cached_value",
                      "number_format", "empty_kind")
MAX_CONTEXT_CELLS = 128


def canonical_table_json(value: Any) -> str:
    """The single canonical encoding used for table citation hashes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def cell_snapshot(cell: Mapping[str, Any]) -> Dict[str, Any]:
    """Return exactly the six snapshot keys of one read-back cell."""
    return {key: cell.get(key) for key in CELL_SNAPSHOT_KEYS}


def _context_pairs(ref: Mapping[str, Any]) -> list:
    pairs = {(int(row), int(column)) for row, column in (ref.get("context_cells") or [])}
    return [[row, column] for row, column in sorted(pairs)]


def build_table_citation_record(ref: Mapping[str, Any],
                                cell: Mapping[str, Any]) -> Dict[str, Any]:
    """Build one table citation record: claim id, excerpt and value hash."""
    snapshot = cell_snapshot(cell)
    context_cells = _context_pairs(ref)
    row_index, column_index = int(ref.get("row")), int(ref.get("column"))
    identity = {
        "revision_id": ref.get("revision_id"), "source_id": ref.get("source_id"),
        "document_id": ref.get("document_id"), "path": ref.get("path"),
        "table_id": ref.get("table_id"), "interpretation_id": ref.get("interpretation_id"),
        "sheet_id": ref.get("sheet_id"), "row": row_index, "column": column_index,
        "context_cells": context_cells, "cell_snapshot": snapshot,
    }
    value = (snapshot.get("cached_value")
             if snapshot.get("formula") and snapshot.get("cached_value") is not None
             else snapshot.get("raw_value"))
    excerpt = canonical_table_json({
        "table_id": ref.get("table_id"),
        "coordinate": cell.get("coordinate") or "R{}C{}".format(row_index, column_index),
        "row": row_index, "column": column_index, "value": value,
        "formula": snapshot.get("formula"),
        "empty_kind": snapshot.get("empty_kind") or ("blank" if value is None else "value"),
    })
    return {
        "claim_id": "table_cell_" + hashlib.sha256(
            canonical_table_json(identity).encode("utf-8")).hexdigest(),
        "excerpt": excerpt,
        "value_sha256": hashlib.sha256(
            canonical_table_json(snapshot).encode("utf-8")).hexdigest(),
        "cell_snapshot": snapshot,
        "context_cells": context_cells,
    }


def build_table_note_record(ref: Mapping[str, Any],
                            note: Mapping[str, Any]) -> Dict[str, Any]:
    """Build one table-note citation record: claim id, excerpt and text hash."""
    text = str(note.get("text") or "")
    note_id = str(note.get("id") or ref.get("note_id") or "")
    identity = {
        "revision_id": ref.get("revision_id"), "source_id": ref.get("source_id"),
        "document_id": ref.get("document_id"), "path": ref.get("path"),
        "table_id": ref.get("table_id"), "interpretation_id": ref.get("interpretation_id"),
        "sheet_id": ref.get("sheet_id"), "note_id": note_id,
        "note_kind": str(note.get("kind") or ref.get("note_kind") or ""), "text": text,
    }
    excerpt = canonical_table_json({
        "table_id": ref.get("table_id"), "note_id": note_id,
        "note_kind": identity["note_kind"], "text": text,
    })
    return {
        "claim_id": "table_note_" + hashlib.sha256(
            canonical_table_json(identity).encode("utf-8")).hexdigest(),
        "excerpt": excerpt,
        "value_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


__all__ = ["CELL_SNAPSHOT_KEYS", "MAX_CONTEXT_CELLS", "TABLE_CELL_KIND",
           "TABLE_NOTE_KIND", "build_table_citation_record",
           "build_table_note_record", "canonical_table_json", "cell_snapshot"]
