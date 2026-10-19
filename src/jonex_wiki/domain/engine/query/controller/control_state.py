"""Pure, replayable hop-control state for LLM-Wiki+ queries.

The query evidence reducer owns what has been read and what is currently
active. This controller consumes explicit snapshots of that active window and
adds the reasoning-control facts that the evidence reducer intentionally does
not own: which hop is running, which required slot an evidence handle supports,
which bridge value may enter the next query, whether candidate search made
progress, and why a run terminated.

There is no model, clock, environment, filesystem, or network access here.
Policy code decides which candidates are high quality and which slots to plan;
this reducer only validates and applies typed events. Replaying the same event
sequence therefore yields the same controller state.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import Any, Dict, Iterable, Literal, Mapping, Optional, Tuple, Union

from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict

from jonex_wiki.domain.engine.query.controller.evidence_state import EvidenceRecord


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
class FailureClass(str, Enum):
    """The mutually exclusive terminal failure layers frozen by CTRL-001."""

    COMPILE_MISSING = "compile_missing"
    RETRIEVAL_MISS = "retrieval_miss"
    REASONING_MISS = "reasoning_miss"
    ANSWER_FORMAT_MISS = "answer_format_miss"
    EVIDENCE_STATE_MISS = "evidence_state_miss"


class RequiredSlot(_FrozenModel):
    """One fact that must be evidence-bound before advancing or answering."""

    slot_id: str
    description: str
    role: Literal["bridge", "fact"] = "fact"
    required_for: Literal["advance", "answer"] = "answer"

    @field_validator("slot_id", "description")
    def _non_blank(cls, value: str, field: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("CONTROL_SLOT_INVALID:" + field.field_name)
        return value

    @model_validator(mode="after")
    def _bridge_advances(self):
        if self.role == "bridge" and self.required_for != "advance":
            raise ValueError("CONTROL_BRIDGE_SLOT_MUST_ADVANCE")
        return self


def _valid_slots(values: Tuple[RequiredSlot, ...]) -> Tuple[RequiredSlot, ...]:
    if not values:
        raise ValueError("CONTROL_SLOTS_EMPTY")
    ids = [slot.slot_id for slot in values]
    if len(set(ids)) != len(ids):
        raise ValueError("CONTROL_SLOT_IDS_DUPLICATE")
    return values


class HopStarted(_FrozenModel):
    event_type: Literal["hop_started"] = "hop_started"
    hop_id: str
    query: str
    slots: Tuple[RequiredSlot, ...]

    @field_validator("hop_id", "query")
    def _non_blank(cls, value: str, field: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("CONTROL_HOP_INVALID:" + field.field_name)
        return value

    _validate_slots = field_validator("slots")(_valid_slots)


class EvidenceWindowUpdated(_FrozenModel):
    """A query-state-linked active evidence snapshot.

    query_event_count is the event count of the authoritative query evidence
    state that produced this window. It must advance monotonically. Exact
    evidence records make handle support independently replayable.
    """

    event_type: Literal["evidence_window"] = "evidence_window"
    query_event_count: int = Field(ge=1)
    active_evidence: Tuple[EvidenceRecord, ...] = ()
    seen_evidence: Tuple[EvidenceRecord, ...] = ()
    seen_evidence_order: Tuple[str, ...] = ()

    @field_validator("active_evidence")
    def _unique_active(
        cls,
        values: Tuple[EvidenceRecord, ...],
    ) -> Tuple[EvidenceRecord, ...]:
        ids = [record.evidence_id for record in values]
        if len(set(ids)) != len(ids):
            raise ValueError("CONTROL_ACTIVE_EVIDENCE_DUPLICATE")
        return values


class SearchProgressObserved(_FrozenModel):
    """One search frontier with caller-classified high-quality candidates."""

    event_type: Literal["search_progress"] = "search_progress"
    hop_id: str
    query: str
    candidate_paths: Tuple[str, ...] = ()
    high_quality_candidate_paths: Tuple[str, ...] = ()
    unread_candidate_paths: Tuple[str, ...] = ()

    @field_validator("hop_id", "query")
    def _non_blank(cls, value: str, field: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("CONTROL_SEARCH_INVALID:" + field.field_name)
        return value

    @field_validator(
        "candidate_paths",
        "high_quality_candidate_paths",
        "unread_candidate_paths",
    )
    def _unique_paths(
        cls,
        values: Tuple[str, ...],
        field: Any,
    ) -> Tuple[str, ...]:
        if any(not isinstance(path, str) or not path.strip() for path in values):
            raise ValueError("CONTROL_SEARCH_PATH_INVALID:" + field.field_name)
        if len(set(values)) != len(values):
            raise ValueError("CONTROL_SEARCH_PATH_DUPLICATE:" + field.field_name)
        return values

    @model_validator(mode="after")
    def _path_subsets(self):
        candidates = set(getattr(self, "candidate_paths", ()))
        if any(
            path not in candidates
            for path in getattr(self, "high_quality_candidate_paths", ())
        ):
            raise ValueError("CONTROL_HIGH_QUALITY_NOT_CANDIDATE")
        if any(
            path not in candidates
            for path in getattr(self, "unread_candidate_paths", ())
        ):
            raise ValueError("CONTROL_UNREAD_NOT_CANDIDATE")
        return self


class FrontierReadObserved(_FrozenModel):
    """Consume paths from the current hop's accumulated unread frontier."""

    event_type: Literal["frontier_read"] = "frontier_read"
    hop_id: str
    paths: Tuple[str, ...]

    @field_validator("hop_id")
    def _non_blank_hop(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("CONTROL_FRONTIER_READ_INVALID:hop_id")
        return value

    @field_validator("paths")
    def _valid_paths(cls, values: Tuple[str, ...]) -> Tuple[str, ...]:
        if not values:
            raise ValueError("CONTROL_FRONTIER_READ_EMPTY")
        if any(not isinstance(path, str) or not path.strip() for path in values):
            raise ValueError("CONTROL_FRONTIER_READ_INVALID:paths")
        if len(set(values)) != len(values):
            raise ValueError("CONTROL_FRONTIER_READ_DUPLICATE")
        return values


_HANDLE_RE = re.compile(r"^\[E([1-9][0-9]*)\]$")


class SlotEvidenceBound(_FrozenModel):
    """Bind one slot value to one handle over the delivered-material ledger."""

    event_type: Literal["slot_bound"] = "slot_bound"
    hop_id: str
    slot_id: str
    evidence_handle: str
    evidence_id: str
    value: str

    @field_validator("hop_id", "slot_id", "evidence_id", "value")
    def _non_blank(cls, value: str, field: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("CONTROL_BINDING_INVALID:" + field.field_name)
        return value

    @field_validator("evidence_handle")
    def _valid_handle(cls, value: str) -> str:
        if not isinstance(value, str) or _HANDLE_RE.fullmatch(value) is None:
            raise ValueError("CONTROL_HANDLE_INVALID")
        return value


class SlotClosed(_FrozenModel):
    event_type: Literal["slot_closed"] = "slot_closed"
    hop_id: str
    slot_id: str

    @field_validator("hop_id", "slot_id")
    def _non_blank(cls, value: str, field: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("CONTROL_SLOT_CLOSE_INVALID:" + field.field_name)
        return value


class HopAdvanced(_FrozenModel):
    event_type: Literal["hop_advanced"] = "hop_advanced"
    from_hop_id: str
    hop_id: str
    query: str
    slots: Tuple[RequiredSlot, ...]

    @field_validator("from_hop_id", "hop_id", "query")
    def _non_blank(cls, value: str, field: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("CONTROL_HOP_ADVANCE_INVALID:" + field.field_name)
        return value

    _validate_slots = field_validator("slots")(_valid_slots)

    @model_validator(mode="after")
    def _different_hop(self):
        if self.from_hop_id == self.hop_id:
            raise ValueError("CONTROL_HOP_ID_REUSED")
        return self


class RunTerminal(_FrozenModel):
    event_type: Literal["run_terminal"] = "run_terminal"
    status: Literal["answered", "incomplete"]
    failure_class: Optional[FailureClass] = None

    @model_validator(mode="after")
    def _status_matches_failure(self):
        status = self.status
        failure = self.failure_class
        if status == "answered" and failure is not None:
            raise ValueError("CONTROL_ANSWERED_WITH_FAILURE")
        if status == "incomplete" and failure is None:
            raise ValueError("CONTROL_INCOMPLETE_WITHOUT_FAILURE")
        return self


ControlEvent = Union[
    HopStarted,
    EvidenceWindowUpdated,
    SearchProgressObserved,
    FrontierReadObserved,
    SlotEvidenceBound,
    SlotClosed,
    HopAdvanced,
    RunTerminal,
]

_EVENT_TYPES = {
    "hop_started": HopStarted,
    "evidence_window": EvidenceWindowUpdated,
    "search_progress": SearchProgressObserved,
    "frontier_read": FrontierReadObserved,
    "slot_bound": SlotEvidenceBound,
    "slot_closed": SlotClosed,
    "hop_advanced": HopAdvanced,
    "run_terminal": RunTerminal,
}


class EvidenceBinding(_FrozenModel):
    evidence_handle: str
    evidence_id: str
    value: str


class HopState(_FrozenModel):
    hop_id: str
    query: str
    slots: Tuple[RequiredSlot, ...]
    evidence_ids_at_start: Tuple[str, ...] = ()
    bindings_by_slot: Dict[str, Tuple[EvidenceBinding, ...]] = Field(
        default_factory=dict
    )
    closed_slot_ids: Tuple[str, ...] = ()
    searches: Tuple[SearchProgressObserved, ...] = ()

    @model_validator(mode="after")
    def _consistent_hop(self):
        slots = tuple(getattr(self, "slots", ()))
        slot_ids = [slot.slot_id for slot in slots]
        if not slots or len(set(slot_ids)) != len(slot_ids):
            raise ValueError("CONTROL_HOP_SLOTS_INVALID")
        bindings = getattr(self, "bindings_by_slot", {})
        closed = tuple(getattr(self, "closed_slot_ids", ()))
        if any(slot_id not in slot_ids for slot_id in bindings):
            raise ValueError("CONTROL_HOP_BINDING_SLOT_UNKNOWN")
        if len(set(closed)) != len(closed):
            raise ValueError("CONTROL_HOP_CLOSED_SLOT_DUPLICATE")
        if any(slot_id not in slot_ids for slot_id in closed):
            raise ValueError("CONTROL_HOP_CLOSED_SLOT_UNKNOWN")
        if any(not bindings.get(slot_id) for slot_id in closed):
            raise ValueError("CONTROL_HOP_CLOSED_WITHOUT_BINDING")
        baseline = tuple(getattr(self, "evidence_ids_at_start", ()))
        if len(set(baseline)) != len(baseline):
            raise ValueError("CONTROL_HOP_BASELINE_EVIDENCE_DUPLICATE")
        return self

    def slot(self, slot_id: str) -> RequiredSlot:
        for slot in self.slots:
            if slot.slot_id == slot_id:
                return slot
        raise ValueError("CONTROL_SLOT_UNKNOWN")


class ControlState(_FrozenModel):
    """Materialized controller state obtained only from typed events."""

    hops_by_id: Dict[str, HopState] = Field(default_factory=dict)
    hop_order: Tuple[str, ...] = ()
    current_hop_id: Optional[str] = None
    seen_evidence_by_id: Dict[str, EvidenceRecord] = Field(default_factory=dict)
    seen_evidence_order: Tuple[str, ...] = ()
    active_evidence_ids: Tuple[str, ...] = ()
    query_event_count: int = Field(default=0, ge=0)
    seen_high_quality_candidate_paths: Tuple[str, ...] = ()
    unread_frontier_paths: Tuple[str, ...] = ()
    no_progress_searches: int = Field(default=0, ge=0)
    terminal: Optional[RunTerminal] = None
    event_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _consistent_state(self):
        hops = getattr(self, "hops_by_id", {})
        order = tuple(getattr(self, "hop_order", ()))
        current = self.current_hop_id
        if len(set(order)) != len(order) or set(order) != set(hops):
            raise ValueError("CONTROL_HOP_ORDER_INVALID")
        if order:
            if current != order[-1]:
                raise ValueError("CONTROL_CURRENT_HOP_INVALID")
        elif current is not None:
            raise ValueError("CONTROL_CURRENT_HOP_WITHOUT_HOPS")

        seen = getattr(self, "seen_evidence_by_id", {})
        seen_order = tuple(getattr(self, "seen_evidence_order", ()))
        active = tuple(getattr(self, "active_evidence_ids", ()))
        for evidence_id, record in seen.items():
            if evidence_id != record.evidence_id:
                raise ValueError("CONTROL_EVIDENCE_KEY_MISMATCH")
        if len(set(seen_order)) != len(seen_order) or set(seen_order) != set(seen):
            raise ValueError("CONTROL_SEEN_ORDER_INVALID")
        if len(set(active)) != len(active):
            raise ValueError("CONTROL_ACTIVE_EVIDENCE_DUPLICATE")
        if any(evidence_id not in seen for evidence_id in active):
            raise ValueError("CONTROL_ACTIVE_EVIDENCE_UNKNOWN")
        for hop_id, hop in hops.items():
            if hop_id != hop.hop_id:
                raise ValueError("CONTROL_HOP_KEY_MISMATCH")
            if any(
                evidence_id not in seen
                for evidence_id in hop.evidence_ids_at_start
            ):
                raise ValueError("CONTROL_HOP_BASELINE_EVIDENCE_UNKNOWN")
            if any(
                binding.evidence_id not in seen
                for bindings in hop.bindings_by_slot.values()
                for binding in bindings
            ):
                raise ValueError("CONTROL_HOP_BINDING_EVIDENCE_UNKNOWN")

        seen_candidates = tuple(
            getattr(self, "seen_high_quality_candidate_paths", ())
        )
        frontier = tuple(getattr(self, "unread_frontier_paths", ()))
        if len(set(seen_candidates)) != len(seen_candidates):
            raise ValueError("CONTROL_SEEN_CANDIDATE_DUPLICATE")
        if len(set(frontier)) != len(frontier):
            raise ValueError("CONTROL_FRONTIER_DUPLICATE")
        return self


def initial_control_state() -> ControlState:
    return ControlState()


def parse_control_event(event: object) -> ControlEvent:
    if isinstance(event, tuple(_EVENT_TYPES.values())):
        return event
    if not isinstance(event, Mapping):
        raise ValueError("CONTROL_EVENT_NOT_OBJECT")
    event_type = event.get("event_type")
    model = _EVENT_TYPES.get(event_type)
    if model is None:
        raise ValueError("CONTROL_EVENT_TYPE_UNKNOWN")
    return model.model_validate(dict(event))


def _normalize_binding_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _new_state(
    state: ControlState,
    *,
    hops_by_id: Optional[Dict[str, HopState]] = None,
    hop_order: Optional[Tuple[str, ...]] = None,
    current_hop_id: Optional[str] = None,
    keep_current_hop: bool = True,
    seen_evidence_by_id: Optional[Dict[str, EvidenceRecord]] = None,
    seen_evidence_order: Optional[Tuple[str, ...]] = None,
    active_evidence_ids: Optional[Tuple[str, ...]] = None,
    query_event_count: Optional[int] = None,
    seen_high_quality_candidate_paths: Optional[Tuple[str, ...]] = None,
    unread_frontier_paths: Optional[Tuple[str, ...]] = None,
    no_progress_searches: Optional[int] = None,
    terminal: Optional[RunTerminal] = None,
    keep_terminal: bool = True,
) -> ControlState:
    return ControlState(
        hops_by_id=state.hops_by_id if hops_by_id is None else hops_by_id,
        hop_order=state.hop_order if hop_order is None else hop_order,
        current_hop_id=(
            state.current_hop_id if keep_current_hop else current_hop_id
        ),
        seen_evidence_by_id=(
            state.seen_evidence_by_id
            if seen_evidence_by_id is None
            else seen_evidence_by_id
        ),
        seen_evidence_order=(
            state.seen_evidence_order
            if seen_evidence_order is None
            else seen_evidence_order
        ),
        active_evidence_ids=(
            state.active_evidence_ids
            if active_evidence_ids is None
            else active_evidence_ids
        ),
        query_event_count=(
            state.query_event_count
            if query_event_count is None
            else query_event_count
        ),
        seen_high_quality_candidate_paths=(
            state.seen_high_quality_candidate_paths
            if seen_high_quality_candidate_paths is None
            else seen_high_quality_candidate_paths
        ),
        unread_frontier_paths=(
            state.unread_frontier_paths
            if unread_frontier_paths is None
            else unread_frontier_paths
        ),
        no_progress_searches=(
            state.no_progress_searches
            if no_progress_searches is None
            else no_progress_searches
        ),
        terminal=state.terminal if keep_terminal else terminal,
        event_count=state.event_count + 1,
    )


def _replace_hop(
    state: ControlState,
    hop: HopState,
) -> Dict[str, HopState]:
    hops = dict(state.hops_by_id)
    hops[hop.hop_id] = hop
    return hops


def reduce_control_state(
    state: ControlState,
    event: object,
) -> ControlState:
    """Apply one typed event without mutating the input state."""

    if state.terminal is not None:
        raise ValueError("CONTROL_STATE_TERMINAL")
    parsed = parse_control_event(event)

    if isinstance(parsed, HopStarted):
        if state.hop_order:
            raise ValueError("CONTROL_HOP_ALREADY_STARTED")
        hop = HopState(
            hop_id=parsed.hop_id,
            query=parsed.query,
            slots=parsed.slots,
        )
        return _new_state(
            state,
            hops_by_id={hop.hop_id: hop},
            hop_order=(hop.hop_id,),
            current_hop_id=hop.hop_id,
            keep_current_hop=False,
        )

    if state.current_hop_id is None:
        raise ValueError("CONTROL_HOP_NOT_STARTED")
    current = state.hops_by_id[state.current_hop_id]

    if isinstance(parsed, EvidenceWindowUpdated):
        if parsed.query_event_count <= state.query_event_count:
            raise ValueError("CONTROL_QUERY_EVENT_COUNT_NOT_MONOTONIC")
        seen = {
            record.evidence_id: record
            for record in parsed.seen_evidence
        }
        return _new_state(
            state,
            seen_evidence_by_id=seen,
            seen_evidence_order=parsed.seen_evidence_order,
            active_evidence_ids=tuple(
                record.evidence_id for record in parsed.active_evidence
            ),
            query_event_count=parsed.query_event_count,
        )

    if isinstance(parsed, SearchProgressObserved):
        if parsed.hop_id != state.current_hop_id:
            raise ValueError("CONTROL_SEARCH_HOP_MISMATCH")
        seen_paths = list(state.seen_high_quality_candidate_paths)
        new_paths = [
            path
            for path in parsed.high_quality_candidate_paths
            if path not in seen_paths
        ]
        seen_paths.extend(new_paths)
        new_unread_paths = [
            path for path in new_paths if path in parsed.unread_candidate_paths
        ]
        unread_frontier = list(state.unread_frontier_paths)
        unread_frontier.extend(
            path
            for path in parsed.unread_candidate_paths
            if path not in unread_frontier
        )
        updated_hop = HopState(
            hop_id=current.hop_id,
            query=current.query,
            slots=current.slots,
            evidence_ids_at_start=current.evidence_ids_at_start,
            bindings_by_slot=current.bindings_by_slot,
            closed_slot_ids=current.closed_slot_ids,
            searches=current.searches + (parsed,),
        )
        return _new_state(
            state,
            hops_by_id=_replace_hop(state, updated_hop),
            seen_high_quality_candidate_paths=tuple(seen_paths),
            unread_frontier_paths=tuple(unread_frontier),
            no_progress_searches=(
                0 if new_unread_paths else state.no_progress_searches + 1
            ),
        )

    if isinstance(parsed, FrontierReadObserved):
        if parsed.hop_id != state.current_hop_id:
            raise ValueError("CONTROL_FRONTIER_READ_HOP_MISMATCH")
        frontier = set(state.unread_frontier_paths)
        if any(path not in frontier for path in parsed.paths):
            raise ValueError("CONTROL_FRONTIER_READ_UNKNOWN")
        consumed = set(parsed.paths)
        return _new_state(
            state,
            unread_frontier_paths=tuple(
                path
                for path in state.unread_frontier_paths
                if path not in consumed
            ),
            no_progress_searches=0,
        )

    if isinstance(parsed, SlotEvidenceBound):
        if parsed.hop_id != state.current_hop_id:
            raise ValueError("CONTROL_BINDING_HOP_MISMATCH")
        slot = current.slot(parsed.slot_id)
        if parsed.slot_id in current.closed_slot_ids:
            raise ValueError("CONTROL_SLOT_ALREADY_CLOSED")

        match = _HANDLE_RE.fullmatch(parsed.evidence_handle)
        assert match is not None
        handle_index = int(match.group(1)) - 1
        # Handles are numbered over the full delivered-material ledger (read
        # order), not the bounded active window, so a binding survives its
        # evidence leaving the recent window (READING-REPAIR §1).  This mirrors
        # ``_resolve_handle`` and ``controller_payload`` in controller.py.
        if handle_index >= len(state.seen_evidence_order):
            raise ValueError("CONTROL_HANDLE_UNKNOWN")
        resolved_id = state.seen_evidence_order[handle_index]
        if resolved_id != parsed.evidence_id:
            raise ValueError("CONTROL_HANDLE_EVIDENCE_MISMATCH")
        # One passage may support more than one distinct relation. Progress is
        # the newly closed slot/candidate edge, not whether the evidence bytes
        # were first read during this hop (Milestone-1 H03).
        record = state.seen_evidence_by_id[resolved_id]
        normalized_value = _normalize_binding_text(parsed.value)
        normalized_excerpt = _normalize_binding_text(record.excerpt)
        if not normalized_value or normalized_value not in normalized_excerpt:
            raise ValueError("CONTROL_BINDING_UNSUPPORTED")

        existing = list(current.bindings_by_slot.get(parsed.slot_id, ()))
        if any(binding.evidence_id == parsed.evidence_id for binding in existing):
            raise ValueError("CONTROL_BINDING_DUPLICATE")
        if slot.role == "bridge" and existing:
            raise ValueError("CONTROL_BRIDGE_ALREADY_BOUND")
        existing.append(
            EvidenceBinding(
                evidence_handle=parsed.evidence_handle,
                evidence_id=parsed.evidence_id,
                value=parsed.value,
            )
        )
        bindings = dict(current.bindings_by_slot)
        bindings[parsed.slot_id] = tuple(existing)
        updated_hop = HopState(
            hop_id=current.hop_id,
            query=current.query,
            slots=current.slots,
            evidence_ids_at_start=current.evidence_ids_at_start,
            bindings_by_slot=bindings,
            closed_slot_ids=current.closed_slot_ids,
            searches=current.searches,
        )
        return _new_state(
            state,
            hops_by_id=_replace_hop(state, updated_hop),
        )

    if isinstance(parsed, SlotClosed):
        if parsed.hop_id != state.current_hop_id:
            raise ValueError("CONTROL_SLOT_CLOSE_HOP_MISMATCH")
        current.slot(parsed.slot_id)
        if parsed.slot_id in current.closed_slot_ids:
            raise ValueError("CONTROL_SLOT_ALREADY_CLOSED")
        slot_bindings = current.bindings_by_slot.get(parsed.slot_id, ())
        if not slot_bindings:
            raise ValueError("CONTROL_SLOT_WITHOUT_BINDING")
        # A binding is valid against the full delivered-material ledger, not
        # just the bounded active window: leaving the recent window is message
        # compression, not source invalidation (READING-REPAIR §1).
        seen_evidence_ids = set(state.seen_evidence_by_id)
        if any(
            binding.evidence_id not in seen_evidence_ids
            for binding in slot_bindings
        ):
            raise ValueError("CONTROL_SLOT_EVIDENCE_INACTIVE")
        updated_hop = HopState(
            hop_id=current.hop_id,
            query=current.query,
            slots=current.slots,
            evidence_ids_at_start=current.evidence_ids_at_start,
            bindings_by_slot=current.bindings_by_slot,
            closed_slot_ids=current.closed_slot_ids + (parsed.slot_id,),
            searches=current.searches,
        )
        return _new_state(
            state,
            hops_by_id=_replace_hop(state, updated_hop),
        )

    if isinstance(parsed, HopAdvanced):
        if parsed.from_hop_id != state.current_hop_id:
            raise ValueError("CONTROL_ADVANCE_HOP_MISMATCH")
        if parsed.hop_id in state.hops_by_id:
            raise ValueError("CONTROL_HOP_ID_REUSED")
        advance_slots = [
            slot
            for slot in current.slots
            if slot.required_for == "advance"
        ]
        if not any(slot.role == "bridge" for slot in advance_slots):
            raise ValueError("CONTROL_ADVANCE_BRIDGE_SLOT_MISSING")
        open_slots = [
            slot.slot_id
            for slot in advance_slots
            if slot.slot_id not in current.closed_slot_ids
        ]
        if open_slots:
            raise ValueError("CONTROL_ADVANCE_SLOT_OPEN")
        active_evidence_ids = set(state.active_evidence_ids)
        advance_evidence_ids = {
            binding.evidence_id
            for slot in advance_slots
            for binding in current.bindings_by_slot.get(slot.slot_id, ())
        }
        if not advance_evidence_ids.issubset(active_evidence_ids):
            raise ValueError("CONTROL_ADVANCE_EVIDENCE_INACTIVE")
        normalized_query = _normalize_binding_text(parsed.query)
        for slot in advance_slots:
            if slot.role != "bridge":
                continue
            for binding in current.bindings_by_slot.get(slot.slot_id, ()):
                if _normalize_binding_text(binding.value) not in normalized_query:
                    raise ValueError("CONTROL_NEXT_QUERY_MISSING_BRIDGE")
        hop = HopState(
            hop_id=parsed.hop_id,
            query=parsed.query,
            slots=parsed.slots,
            evidence_ids_at_start=state.active_evidence_ids,
        )
        hops = dict(state.hops_by_id)
        hops[hop.hop_id] = hop
        return _new_state(
            state,
            hops_by_id=hops,
            hop_order=state.hop_order + (hop.hop_id,),
            current_hop_id=hop.hop_id,
            keep_current_hop=False,
            unread_frontier_paths=(),
            no_progress_searches=0,
        )

    if isinstance(parsed, RunTerminal):
        if parsed.status == "answered":
            answer_slots = [
                (hop, slot)
                for hop_id in state.hop_order
                for hop in (state.hops_by_id[hop_id],)
                for slot in hop.slots
                if slot.required_for == "answer"
            ]
            if not answer_slots:
                raise ValueError("CONTROL_ANSWER_SLOT_MISSING")
            if any(
                slot.slot_id not in hop.closed_slot_ids
                for hop, slot in answer_slots
            ):
                raise ValueError("CONTROL_ANSWER_SLOT_OPEN")
            # Answer bindings are checked against the full delivered-material
            # ledger, not the bounded active window (READING-REPAIR §1).
            seen = set(state.seen_evidence_by_id)
            bound_ids = {
                binding.evidence_id
                for hop_id in state.hop_order
                for bindings in state.hops_by_id[
                    hop_id
                ].bindings_by_slot.values()
                for binding in bindings
            }
            if not bound_ids.issubset(seen):
                raise ValueError("CONTROL_ANSWER_EVIDENCE_INACTIVE")
        return _new_state(
            state,
            terminal=parsed,
            keep_terminal=False,
        )

    raise AssertionError("unreachable control event")


def replay_control_events(events: Iterable[object]) -> ControlState:
    state = initial_control_state()
    for event in events:
        state = reduce_control_state(state, event)
    return state


__all__ = [
    "ControlEvent",
    "ControlState",
    "EvidenceBinding",
    "EvidenceWindowUpdated",
    "FailureClass",
    "FrontierReadObserved",
    "HopAdvanced",
    "HopStarted",
    "HopState",
    "RequiredSlot",
    "RunTerminal",
    "SearchProgressObserved",
    "SlotClosed",
    "SlotEvidenceBound",
    "initial_control_state",
    "parse_control_event",
    "reduce_control_state",
    "replay_control_events",
]
