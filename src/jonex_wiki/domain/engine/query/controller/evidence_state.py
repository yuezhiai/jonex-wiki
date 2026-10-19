"""Pure, replayable evidence state for LLM-Wiki+ queries.

The traversal loop may discover many candidates, but a fact becomes citable
only after a successful read registers its exact six-field evidence record.
This module keeps that append-only ``seen`` registry separate from the bounded
``active`` set exposed at a synthesis boundary.  Every transition is a typed
event, and reducing the same event sequence always yields the same state.

There is deliberately no model, clock, environment, filesystem, or network
access here.  Controller policy belongs to the caller; the reducer only checks
and applies explicit transitions.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, Literal, Mapping, Optional, Tuple, Union

from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict


EVIDENCE_FIELDS = (
    "claim_id",
    "passage_id",
    "source_id",
    "document_id",
    "chunk_id",
    "excerpt",
)

# 承载该证据的 Wiki 页路径（如 concepts/xxx.md / sources/xxx.md）。
# 只用于调用方「来源引用 ↔ Wiki 页」定位；**不在 EVIDENCE_FIELDS 内**——证据身份
# （canonical_evidence_id）仍由六个公开引用字段决定，加路径不改任何已注册证据的 id，
# 也不改模型可见的引用契约（模型仍只见到六个字段，见 _active_evidence_payload）。
PAGE_PATH_FIELD = "page_path"


def canonical_evidence_id(record: Mapping[str, Any]) -> str:
    """Return the content-addressed ID for one exact evidence record.

    The digest binds all six public citation fields without trimming or Unicode
    normalization.  Consequently a different excerpt, identifier, whitespace,
    or code point is a different piece of evidence.  Sorted compact JSON makes
    the result independent of input mapping order.
    """

    payload: Dict[str, str] = {}
    for field in EVIDENCE_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("EVIDENCE_FIELD_INVALID:" + field)
        payload[field] = value
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "ev_" + hashlib.sha256(encoded).hexdigest()


def _identity_dump(record: "EvidenceRecord") -> Dict[str, str]:
    """The identity-bearing subset of a record, i.e. exactly what the id binds.

    Used by the registry's conflict check so that it compares the same fields
    ``canonical_evidence_id`` hashes, instead of the whole record (which also
    carries presentation metadata such as ``page_path``).
    """

    return {field: getattr(record, field) for field in EVIDENCE_FIELDS}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
class EvidenceRecord(_FrozenModel):
    """One exact, model-readable and backend-citable evidence unit."""

    evidence_id: str = ""
    claim_id: str
    passage_id: str
    source_id: str
    document_id: str
    chunk_id: str
    excerpt: str
    # 携带该证据的 Wiki 页路径；缺省空串（表格句柄等无单一页时留空）。
    page_path: str = ""

    @model_validator(mode="before")
    def _bind_canonical_id(cls, values: Any) -> Dict[str, Any]:
        if not isinstance(values, dict):
            raise ValueError("EVIDENCE_RECORD_NOT_OBJECT")
        normalized = dict(values)
        expected = canonical_evidence_id(normalized)
        supplied = normalized.get("evidence_id", "")
        if supplied and supplied != expected:
            raise ValueError("EVIDENCE_ID_MISMATCH")
        normalized["evidence_id"] = expected
        return normalized

    @field_validator(*EVIDENCE_FIELDS)
    def _non_blank_exact_string(cls, value: str, field: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("EVIDENCE_FIELD_INVALID:" + field.field_name)
        return value


class SearchCandidate(_FrozenModel):
    """Selection-only search metadata; never a registered fact."""

    path: str
    rank: int = Field(ge=1)
    snippet: str = ""

    @field_validator("path")
    def _path_non_blank(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("SEARCH_CANDIDATE_PATH_INVALID")
        return value


class SearchRecorded(_FrozenModel):
    event_type: Literal["search"] = "search"
    query: str
    candidates: Tuple[SearchCandidate, ...] = ()

    @field_validator("query", mode="before")
    def _query_is_string(cls, value: str) -> str:
        # The frozen wiki_search contract permits an empty string; it is still
        # a real, replayable search that deterministically returns no hits.
        if not isinstance(value, str):
            raise ValueError("SEARCH_QUERY_INVALID")
        return value


class EvidenceRead(_FrozenModel):
    event_type: Literal["read"] = "read"
    paths: Tuple[str, ...]
    evidence: Tuple[EvidenceRecord, ...] = ()

    @field_validator("paths")
    def _valid_paths(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        if not values or len(set(values)) != len(values):
            raise ValueError("READ_PATHS_INVALID")
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("READ_PATHS_INVALID")
        return values


def _unique_event_ids(values: Tuple[str, ...]) -> Tuple[str, ...]:
    if not values or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("EVENT_EVIDENCE_IDS_INVALID")
    if len(set(values)) != len(values):
        raise ValueError("EVENT_EVIDENCE_IDS_DUPLICATE")
    return values


class EvidenceSelected(_FrozenModel):
    event_type: Literal["select"] = "select"
    evidence_ids: Tuple[str, ...]

    _validate_ids = field_validator("evidence_ids")(_unique_event_ids)


class EvidenceDropped(_FrozenModel):
    event_type: Literal["drop"] = "drop"
    evidence_ids: Tuple[str, ...]

    _validate_ids = field_validator("evidence_ids")(_unique_event_ids)


class SynthesisStarted(_FrozenModel):
    event_type: Literal["synthesize"] = "synthesize"
    active_evidence_ids: Tuple[str, ...]

    @field_validator("active_evidence_ids")
    def _unique_snapshot_ids(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("SYNTHESIS_EVIDENCE_IDS_DUPLICATE")
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("SYNTHESIS_EVIDENCE_IDS_INVALID")
        return values


QueryEvent = Union[
    SearchRecorded,
    EvidenceRead,
    EvidenceSelected,
    EvidenceDropped,
    SynthesisStarted,
]

_EVENT_TYPES = {
    "search": SearchRecorded,
    "read": EvidenceRead,
    "select": EvidenceSelected,
    "drop": EvidenceDropped,
    "synthesize": SynthesisStarted,
}


class QueryState(_FrozenModel):
    """Materialized state obtained solely by reducing typed query events."""

    max_active_evidence: int = Field(ge=1)
    seen_evidence_by_id: Dict[str, EvidenceRecord] = Field(default_factory=dict)
    seen_evidence_order: Tuple[str, ...] = ()
    active_evidence_ids: Tuple[str, ...] = ()
    searches: Tuple[SearchRecorded, ...] = ()
    read_paths: Tuple[Tuple[str, ...], ...] = ()
    synthesis_snapshots: Tuple[Tuple[str, ...], ...] = ()
    event_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_state(self):
        seen = getattr(self, "seen_evidence_by_id", {})
        order = tuple(getattr(self, "seen_evidence_order", ()))
        active = tuple(getattr(self, "active_evidence_ids", ()))
        maximum = getattr(self, "max_active_evidence", 0)
        snapshots = tuple(getattr(self, "synthesis_snapshots", ()))

        if len(set(order)) != len(order) or set(order) != set(seen):
            raise ValueError("QUERY_STATE_SEEN_ORDER_INVALID")
        for evidence_id, record in seen.items():
            if evidence_id != record.evidence_id:
                raise ValueError("QUERY_STATE_EVIDENCE_KEY_MISMATCH")
            if canonical_evidence_id(record.model_dump()) != evidence_id:
                raise ValueError("QUERY_STATE_EVIDENCE_ID_MISMATCH")
        if len(set(active)) != len(active):
            raise ValueError("QUERY_STATE_ACTIVE_DUPLICATE")
        if any(evidence_id not in seen for evidence_id in active):
            raise ValueError("QUERY_STATE_ACTIVE_UNKNOWN")
        if len(active) > maximum:
            raise ValueError("QUERY_STATE_ACTIVE_LIMIT_EXCEEDED")
        for snapshot in snapshots:
            if not snapshot:
                raise ValueError("QUERY_STATE_SYNTHESIS_EMPTY")
            if len(set(snapshot)) != len(snapshot):
                raise ValueError("QUERY_STATE_SYNTHESIS_DUPLICATE")
            if any(evidence_id not in seen for evidence_id in snapshot):
                raise ValueError("QUERY_STATE_SYNTHESIS_UNKNOWN")
            if len(snapshot) > maximum:
                raise ValueError("QUERY_STATE_SYNTHESIS_LIMIT_EXCEEDED")
        return self

    def active_evidence(self) -> Tuple[EvidenceRecord, ...]:
        """Return active records in their explicit synthesis order."""

        return tuple(
            self.seen_evidence_by_id[evidence_id]
            for evidence_id in self.active_evidence_ids
        )

    def evidence_handle(self, evidence_id: str) -> Optional[str]:
        """Stable read-order handle ``[E<n>]`` for one delivered record.

        Handles are numbered over ``seen_evidence_order`` (the append-only
        delivered-material ledger), never over the bounded active window.  A
        citation therefore stays valid after its evidence is evicted from the
        recent window; message compression and citation eligibility are
        deliberately different concerns.
        """

        try:
            index = self.seen_evidence_order.index(evidence_id)
        except ValueError:
            return None
        return "[E{}]".format(index + 1)

    def evidence_handle_map(self) -> Dict[str, str]:
        """Map every delivered handle to its evidence_id, in read order."""

        return {
            "[E{}]".format(index + 1): evidence_id
            for index, evidence_id in enumerate(self.seen_evidence_order)
        }


def initial_query_state(*, max_active_evidence: int) -> QueryState:
    """Create the empty state for a deterministic replay."""

    return QueryState(max_active_evidence=max_active_evidence)


def parse_query_event(event: object) -> QueryEvent:
    """Parse a serialized event without a permissive fallback."""

    if isinstance(event, tuple(_EVENT_TYPES.values())):
        return event
    if not isinstance(event, Mapping):
        raise ValueError("QUERY_EVENT_NOT_OBJECT")
    event_type = event.get("event_type")
    model = _EVENT_TYPES.get(event_type)
    if model is None:
        raise ValueError("QUERY_EVENT_TYPE_UNKNOWN")
    return model.model_validate(dict(event))


def reduce_query_state(state: QueryState, event: object) -> QueryState:
    """Apply one event, returning a new state and never mutating ``state``."""

    parsed = parse_query_event(event)
    seen = dict(state.seen_evidence_by_id)
    seen_order = list(state.seen_evidence_order)
    active = list(state.active_evidence_ids)
    searches = list(state.searches)
    read_paths = list(state.read_paths)
    snapshots = list(state.synthesis_snapshots)

    if isinstance(parsed, SearchRecorded):
        searches.append(parsed)

    elif isinstance(parsed, EvidenceRead):
        read_paths.append(parsed.paths)
        for record in parsed.evidence:
            existing = seen.get(record.evidence_id)
            if existing is not None:
                # 冲突判定必须与身份定义保持一致：evidence_id 只由六个公开引用
                # 字段决定（见 canonical_evidence_id / EVIDENCE_FIELDS），page_path
                # 是调用方定位用的展示元数据，不参与身份。同一条断言从两个 Wiki
                # 页（或「表格句柄无单一页」与某个页）读到时，六个字段完全相同、
                # page_path 不同，按整条记录比对会误判冲突并把整次检索打成失败。
                if _identity_dump(existing) != _identity_dump(record):
                    raise ValueError("EVIDENCE_REGISTRY_CONFLICT")
                # 身份相同则证据等价；仅在已有记录缺 page_path 时补上，让引用能
                # 定位到 Wiki 页。非空的 page_path 保持先到先得（reducer 仍是事件
                # 序列的纯函数）。
                if not existing.page_path and record.page_path:
                    seen[record.evidence_id] = record
                continue
            if canonical_evidence_id(record.model_dump()) != record.evidence_id:
                raise ValueError("EVIDENCE_ID_MISMATCH")
            seen[record.evidence_id] = record
            seen_order.append(record.evidence_id)

    elif isinstance(parsed, EvidenceSelected):
        unknown = [
            evidence_id
            for evidence_id in parsed.evidence_ids
            if evidence_id not in seen
        ]
        if unknown:
            raise ValueError("ACTIVE_EVIDENCE_UNKNOWN")
        selected = active + [
            evidence_id
            for evidence_id in parsed.evidence_ids
            if evidence_id not in active
        ]
        if len(selected) > state.max_active_evidence:
            raise ValueError("ACTIVE_EVIDENCE_LIMIT_EXCEEDED")
        active = selected

    elif isinstance(parsed, EvidenceDropped):
        if any(evidence_id not in active for evidence_id in parsed.evidence_ids):
            raise ValueError("ACTIVE_EVIDENCE_NOT_ACTIVE")
        dropped = set(parsed.evidence_ids)
        active = [evidence_id for evidence_id in active if evidence_id not in dropped]

    elif isinstance(parsed, SynthesisStarted):
        if not active:
            raise ValueError("SYNTHESIS_WITHOUT_ACTIVE_EVIDENCE")
        if tuple(parsed.active_evidence_ids) != tuple(active):
            raise ValueError("SYNTHESIS_ACTIVE_MISMATCH")
        snapshots.append(tuple(active))

    return QueryState(
        max_active_evidence=state.max_active_evidence,
        seen_evidence_by_id=seen,
        seen_evidence_order=tuple(seen_order),
        active_evidence_ids=tuple(active),
        searches=tuple(searches),
        read_paths=tuple(read_paths),
        synthesis_snapshots=tuple(snapshots),
        event_count=state.event_count + 1,
    )


def replay_query_events(
    events: Iterable[object],
    *,
    max_active_evidence: int,
) -> QueryState:
    """Reduce a complete serialized or typed event stream from empty state."""

    state = initial_query_state(max_active_evidence=max_active_evidence)
    for event in events:
        state = reduce_query_state(state, event)
    return state


__all__ = [
    "EVIDENCE_FIELDS",
    "EvidenceDropped",
    "EvidenceRead",
    "EvidenceRecord",
    "EvidenceSelected",
    "QueryEvent",
    "QueryState",
    "SearchCandidate",
    "SearchRecorded",
    "SynthesisStarted",
    "canonical_evidence_id",
    "initial_query_state",
    "parse_query_event",
    "reduce_query_state",
    "replay_query_events",
]
