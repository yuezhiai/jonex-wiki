"""Evidence-state helpers for the wiki query engine.

Extracted verbatim from ``原引擎/query_algorithm.py``: the
deterministic evidence gate and compaction helpers (``_evidence_records``,
``_compact_evidence_record``, ``_evidence_key``, ``_cap_evidence_diversity``,
``_compact_claim_for_model``), the query-state event recording bridge
(``_record_query_state_event``, ``_active_evidence_payload``,
``_activate_new_evidence``), the tolerant excerpt normalizer, and the
evidence-budget configuration helpers.  Pure: no provider, network,
filesystem, storage or database access happens here.  The old import path
stays available through the re-export shim.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from jonex_wiki.domain.engine.models.query_models import TraceEntry
from jonex_wiki.domain.engine.compile.content_validation import ValidatedClaim
from jonex_wiki.domain.engine.query.retrieval import _env_int
from jonex_wiki.domain.engine.query.controller.controller import (
    ControllerRuntime,
    sync_evidence_window,
)
from jonex_wiki.domain.engine.query.controller.evidence_state import (
    EVIDENCE_FIELDS,
    EvidenceDropped,
    EvidenceSelected,
    QueryEvent as EvidenceStateEvent,
    QueryState as EvidenceQueryState,
    reduce_query_state,
)


# Wave 3 P0.1: model-facing tool messages bound each claim's evidence.
# Excerpts are truncated to ``EVIDENCE_EXCERPT_LIMIT`` characters and the
# redundant per-claim copies are trimmed to the compact citation fields.
EVIDENCE_EXCERPT_LIMIT = 500
# Model-facing compact fields kept per evidence record (redundant long identity
# fields are dropped from the per-claim copies; the flat allowlist stays full).
EVIDENCE_COMPACT_FIELDS = ("passage_id", "source_id", "excerpt")

# QUERY-001: the evidence registry is unbounded for audit/replay, while the set
# exposed to judge/synthesis and citation validation is explicitly bounded.
#
# The bound has to hold one table page.  A read that returns 56 cells - seven
# rows of an eight-column table, the shape a "which rows carry this code"
# question needs - used to evict 24 of them on arrival, and the model answered
# by re-reading rows until the run exhausted its token budget with no citation
# at all (q003-attempt-014).  The model-facing size is not what it was when 32
# was chosen: the delivered table message now echoes at most twelve citation
# records (the rest are counted, never hidden) and drops per-cell defaults, so
# a larger window costs the prompt almost nothing while removing the eviction
# loop.  Sixty-four cells is one 8x8 page.
MAX_ACTIVE_EVIDENCE = 64
MAX_ACTIVE_EVIDENCE_ENV = "QUERY_MAX_ACTIVE_EVIDENCE"

# Wave 3 P4: bounded evidence units.  Collected evidence is capped after each
# read at ``EVIDENCE_UNITS_MAX`` records, preferring one record per distinct
# document before filling the rest, so the model-facing context stays bounded
# and noisy duplicates cannot accumulate across hops.
EVIDENCE_UNITS_MAX = 12
QUERY_EVIDENCE_UNITS_MAX_ENV = "QUERY_EVIDENCE_UNITS_MAX"
EVIDENCE_PER_DOC_MAX = 6
QUERY_EVIDENCE_PER_DOC_ENV = "QUERY_EVIDENCE_PER_DOC_MAX"


def _evidence_excerpt_limit() -> int:
    return _env_int("QUERY_EVIDENCE_EXCERPT_LIMIT", EVIDENCE_EXCERPT_LIMIT)


def _evidence_units_max() -> int:
    return _env_int(QUERY_EVIDENCE_UNITS_MAX_ENV, EVIDENCE_UNITS_MAX)


def _evidence_per_doc_max() -> int:
    return _env_int(QUERY_EVIDENCE_PER_DOC_ENV, EVIDENCE_PER_DOC_MAX)


def _max_active_evidence() -> int:
    """Read the positive active-evidence bound (default 64)."""

    return _env_int(MAX_ACTIVE_EVIDENCE_ENV, MAX_ACTIVE_EVIDENCE)


# ──────────────────────────────────────────────
#  Evidence gate and corpus construction
# ──────────────────────────────────────────────


def _evidence_records(claim: ValidatedClaim) -> List[Dict[str, str]]:
    """Expand one validated claim into evidence records with all six fields.

    额外带上承载该证据的 Wiki 页路径（claim.page_path）：它不参与证据身份
    （见 EVIDENCE_FIELDS），但调用方要用它把「来源引用」对应回 Wiki 页。
    """
    records: List[Dict[str, str]] = []
    for ref in claim.evidence:
        records.append(
            {
                "claim_id": claim.claim_id,
                "passage_id": ref.passage_id,
                "source_id": ref.source_id,
                "document_id": ref.document_id,
                "chunk_id": ref.chunk_id,
                "excerpt": ref.excerpt,
                "page_path": claim.page_path,
            }
        )
    return records


def _compact_evidence_record(record: Dict[str, Any]) -> Dict[str, str]:
    """Trim one evidence record to the compact model-facing form.

    Keeps only the short citation identity plus a bounded excerpt.  The full
    six-field record remains available on the flat ``evidence`` allowlist and
    in trace spans, so citation/grounding semantics are unchanged.
    """
    excerpt = str(record.get("excerpt", ""))
    return {
        "passage_id": str(record.get("passage_id", "")),
        "source_id": str(record.get("source_id", "")),
        "excerpt": excerpt[:_evidence_excerpt_limit()],
    }


def _evidence_key(record: Dict[str, Any]) -> tuple:
    """A stable identity for one evidence record (identity fields + excerpt)."""
    return (
        str(record.get("claim_id", "")),
        str(record.get("passage_id", "")),
        str(record.get("source_id", "")),
        str(record.get("chunk_id", "")),
        _normalize_excerpt(str(record.get("excerpt", ""))),
    )


def _cap_evidence_diversity(
    records: List[Dict[str, str]],
    limit: int,
    per_doc: int = EVIDENCE_PER_DOC_MAX,
    focus_terms: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    """Cap evidence records while preferring distinct documents and breadth.

    Every distinct document stays represented (one record first), then each
    document fills up to ``per_doc`` records by identity-new order, and finally
    the total is capped at ``limit``.  A global cap alone could evict a whole
    document's evidence (observed: "Gal Gadot" answer correct but its supporting
    record dropped after 12 units), which breaks answer grounding.  Pure and
    deterministic so the same read order yields the same evidence set.

    ``focus_terms`` (S2G-RAG evidence extractor): when supplied, records whose
    excerpt overlaps the active gap/question terms are ranked first (stable;
    ties keep encounter order) before the diversity passes, so newly read
    evidence prioritises the missing fact instead of accumulating noise.
    """
    if limit <= 0 or per_doc <= 0:
        return []
    # A recovered table is bounded by characters at read time. Preserve its
    # complete member passages rather than silently reverting to four windows.
    # Native table cell/note records (table_cell_/table_note_) carry the same
    # per-cell JSON excerpt contract and must be preserved exactly like the
    # legacy source_span_table_ form, otherwise an unrelated wiki_read crops a
    # completed 66-cell request down to four cells.
    table_records = [
        r for r in records
        if str(r.get("claim_id", "")).startswith(("source_span_table_", "table_cell_", "table_note_"))
    ]
    if table_records:
        table_pids = {r["passage_id"] for r in table_records}
        records = [r for r in records if r in table_records or r.get("passage_id") not in table_pids]
        limit = max(limit, min(32, len(table_records)))
        per_doc = max(per_doc, min(32, len(table_records)))
    if focus_terms:
        norm_terms = [t.casefold() for t in focus_terms if t and len(t) >= 2]
        if norm_terms:
            scored = [
                (
                    -sum(
                        1
                        for t in norm_terms
                        if t in _normalize_excerpt(str(rec.get("excerpt", "")))
                    ),
                    idx,
                    rec,
                )
                for idx, rec in enumerate(records)
            ]
            records = [rec for _score, _idx, rec in sorted(scored)]
    # A source-unit record contains the complete parser-preserved row/span,
    # while generated claims from the same document can cover only selected
    # cells.  Put source units first before applying the per-document cap so a
    # long table row cannot lose its decisive trailing columns (for example an
    # age or channel restriction) merely because four derived claims arrived
    # earlier.
    records = sorted(
        enumerate(records),
        key=lambda item: (
            0 if str(item[1].get("claim_id", "")).startswith("source_span_") else 1,
            item[0],
        ),
    )
    records = [record for _index, record in records]
    kept: List[Dict[str, str]] = []
    per_doc_count: Dict[str, int] = {}
    seen_ids = set()
    # First pass: one record per distinct document (encounter order), so no
    # document is ever fully evicted by a global budget.
    for rec in records:
        doc = str(rec.get("document_id", ""))
        if doc in per_doc_count:
            continue
        per_doc_count[doc] = 1
        seen_ids.add(_evidence_key(rec))
        kept.append(rec)
        if len(kept) >= limit:
            return kept
    # Second pass: fill each document up to ``per_doc`` records.
    for rec in records:
        if len(kept) >= limit:
            break
        doc = str(rec.get("document_id", ""))
        if per_doc_count.get(doc, 0) >= per_doc:
            continue
        key = _evidence_key(rec)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        per_doc_count[doc] = per_doc_count.get(doc, 0) + 1
        kept.append(rec)
    return kept


def _compact_claim_for_model(claim_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Return a claim dict with deterministically compacted evidence.

    Per-claim evidence is deduplicated by the compact reference key
    (passage_id, source_id, truncated excerpt) and trimmed to the bounded
    form, so the redundant full copies are not sent to the model for every
    claim.
    """
    compact: List[Dict[str, str]] = []
    seen = set()
    for ref in claim_dict.get("evidence", []):
        compact_rec = _compact_evidence_record(ref)
        key = (
            compact_rec["passage_id"],
            compact_rec["source_id"],
            compact_rec["excerpt"],
        )
        if key in seen:
            continue
        seen.add(key)
        compact.append(compact_rec)
    return {
        "claim_id": claim_dict.get("claim_id"),
        "page_path": claim_dict.get("page_path"),
        "text": claim_dict.get("text"),
        "evidence": compact,
    }


def _record_query_state_event(
    state: Dict[str, Any],
    event: EvidenceStateEvent,
    trace: List[TraceEntry],
) -> None:
    """Reduce and persist one canonical replay event in the public trace."""

    reduced = reduce_query_state(state["query_state"], event)
    payload = event.model_dump(mode="json")
    state["query_state"] = reduced
    trace.append(
        TraceEntry(
            event="query_state_event",
            detail={"sequence": reduced.event_count, "event": payload},
        )
    )
    runtime = state.get("controller")
    if (
        isinstance(runtime, ControllerRuntime)
        and runtime.state.terminal is None
        and reduced.event_count > runtime.state.query_event_count
    ):
        updated, window = sync_evidence_window(runtime, reduced)
        # Deferred import: ``_publish_control_transition`` stays on the legacy
        # shim (controller responsibility, split in) — resolving it at call
        # time avoids an import cycle between the evidence and controller layers.
        from jonex_wiki.domain.engine.query.algorithm import (
            _publish_control_transition as _publish,
        )

        _publish(state, updated, (window,), trace)


def _active_evidence_payload(state: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return exactly the active six-field citation allowlist, in order.

    显式按 EVIDENCE_FIELDS 投影（而不是 dump 全部字段）：EvidenceRecord 还带
    调用方专用的 page_path，绝不能进模型可见的引用白名单——模型一旦照抄它，
    CitationDraft(extra=forbid) 会直接判引用非法。
    """

    query_state: EvidenceQueryState = state["query_state"]
    return [
        {key: str(record.model_dump().get(key, "")) for key in EVIDENCE_FIELDS}
        for record in query_state.active_evidence()
    ]


def pinned_evidence_ids(state):
    runtime = state.get("controller")
    if runtime is None:
        return set()
    return {binding.evidence_id for hop in runtime.state.hops_by_id.values()
            for bindings in hop.bindings_by_slot.values() for binding in bindings}


def _activate_new_evidence(
    state: Dict[str, Any],
    evidence_ids: List[str],
    trace: List[TraceEntry],
) -> None:
    """Select new evidence with explicit FIFO drops at the active bound.

    The registry remains append-only and bound evidence is pinned. Ordinary
    window compaction is recorded without forcing an incomplete answer; the
    final answer still has to satisfy all slots against active evidence.

    A batch that is larger than the window is compacted, not fatal: one
    validated read - a table page of 64 cells against a 32-item window, for
    instance - is not by itself a sign that the evidence is insufficient, and
    stopping there produced runs that ended with zero citations although the
    requested data had just been read.  What was left out stays registered and
    is reported in the trace.  Only a window with no room at all (every slot
    pinned by a binding) keeps the conservative stop, because then no incoming
    evidence can be admitted at all.
    """

    if not evidence_ids:
        return
    query_state: EvidenceQueryState = state["query_state"]
    maximum = query_state.max_active_evidence
    pinned = pinned_evidence_ids(state)
    capacity = max(0, maximum - len(pinned & set(query_state.active_evidence_ids)))
    selectable = list(evidence_ids[:capacity])
    unselected = list(evidence_ids[capacity:])
    overflow = max(
        0,
        len(query_state.active_evidence_ids) + len(selectable) - maximum,
    )
    dropped = [eid for eid in query_state.active_evidence_ids if eid not in pinned][:overflow]

    if dropped:
        _record_query_state_event(
            state,
            EvidenceDropped(evidence_ids=tuple(dropped)),
            trace,
        )
    if selectable:
        _record_query_state_event(
            state,
            EvidenceSelected(evidence_ids=tuple(selectable)),
            trace,
        )

    if dropped or unselected:
        if unselected and capacity == 0:
            state["evidence_budget_exhausted"] = True
        query_state = state["query_state"]
        trace.append(
            TraceEntry(
                event="evidence_budget_exhausted" if state.get("evidence_budget_exhausted") else "evidence_window_compacted",
                detail={
                    "max_active_evidence": maximum,
                    "seen_evidence_count": len(query_state.seen_evidence_by_id),
                    "active_evidence_count": len(query_state.active_evidence_ids),
                    "dropped_evidence_ids": dropped,
                    "unselected_evidence_ids": unselected,
                },
            )
        )


def _normalize_excerpt(text: str) -> str:
    """Casefold and drop whitespace/punctuation for a tolerant excerpt compare.

    A real model rarely reproduces an evidence excerpt byte-for-byte (it drops
    or reorders spaces, converts full-width punctuation, trims trailing
    periods), and the exact ``cit.model_dump() in allow`` check made every Chinese
    answer land on ``missing_verified_citations``.  Normalizing both sides lets
    structurally identical excerpts verify while still failing a fabricated
    excerpt whose content differs.
    """
    return re.sub(
        r"[\s　，。！？；：、…（）()【】\[\]\"'‘’“”\-—_.,!?;:·]+",
        "",
        text.strip().casefold(),
    )
