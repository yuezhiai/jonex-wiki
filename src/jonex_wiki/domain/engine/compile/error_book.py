"""AC-007 Error Book lifecycle and bounded repair.

This module owns batch numbering, Error Book persistence and one bounded repair
round on top of the accepted Algorithm 1 batch runner (AC-006), composing the
paper pipeline as::

    compile batch -> discover errors -> attribute new errors
    -> persist constraints -> inject open constraints into the next batch
    -> periodic bounded repair -> revalidate before close

It implements the three-round finalizer but does not publish a bundle or wire
the legacy engine.  It is pure with respect to the outside world: it only calls
the injected ``ModelPort`` and never touches storage, the filesystem, a
database, a clock, randomness or the network, never mutates caller input, and
never catches or converts ``ModelPort`` exceptions or invalid model schemas.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Sequence, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict

from jonex_wiki.kernel.ports import ModelPort
from ..models.repair_models import Algorithm1BatchResult, PageMutation
from .repair.acceptance import run_algorithm1_batch
from .repair.blocking import _copy_state_for_mutation
from jonex_wiki.domain.engine.models.compiler_state import CompilerState
from jonex_wiki.domain.engine.compile.incremental import (
    RenderIndexDelta,
    WikiRenderIndex,
    WikiRenderIndexOverlay,
    plan_mutation_impact,
)
from jonex_wiki.domain.engine.compile.content_validation import (
    ClaimDraft,
    ContentReport,
    SemanticReview,
    merge_semantic_review,
    normalize_evidence_text,
    validate_claim_evidence,
)
from jonex_wiki.domain.engine.compile.rendering import render_wiki_delta, render_wiki_tree
from jonex_wiki.domain.engine.compile.structural import (
    StructuralIssue,
    StructuralReport,
    validate_update_targets,
    validate_wiki_delta,
    validate_wiki_tree,
)

# ──────────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────────

ERROR_CATEGORIES = (
    "coverage_gap",
    "dangling_link",
    "incomplete_page",
    "malformed_reference",
    "unseen_overwrite",
    "index_inconsistency",
    "unsupported_fact",
    "cross_page_contradiction",
)

# Error Book severity is deliberately independent from the structural/content
# category.  A dangling link and a malformed page are both structural issues,
# but only the latter must stop publication.  ``Quarantine`` keeps unsupported
# content out of the query corpus, ``Advisory`` is reported for follow-up, and
# ``Transient`` is retried locally without becoming a global three-round gate.
ERROR_CLASSES = ("Blocking", "Quarantine", "Advisory", "Transient")
PUBLISH_BLOCKING_CLASSES = frozenset({"Blocking"})
REPAIR_CLASSES = frozenset({"Blocking"})


def classify_error_class(category: str, code: str) -> str:
    """Map a discovered issue to its publication/repair class.

    The mapping is deterministic and code-based so a model cannot downgrade a
    hard structural failure by returning a friendly attribution.  Existing
    bundles that predate this field are read as ``Blocking`` (the safe default).
    """
    normalized = str(code or "").upper()
    if any(
        marker in normalized
        for marker in ("TIMEOUT", "RATE_LIMIT", "PROVIDER", "TRANSPORT", "RETRY")
    ):
        return "Transient"
    if category == "coverage_gap":
        # S4.3: a confirmed compile omission is reported, never publish-blocking,
        # and never part of the global blocking repair loop.
        return "Advisory"
    if category == "dangling_link":
        return "Advisory"
    if category in {"unsupported_fact", "cross_page_contradiction"}:
        return "Quarantine"
    if category == "malformed_reference" and any(
        marker in normalized
        for marker in (
            "EVIDENCE_",
            "CLAIM_",
            "DUPLICATE_CLAIM",
            "SEMANTIC_",
            "UNSUPPORTED_FACT",
            "CROSS_PAGE_",
            "EMPTY_SELECTION",
        )
    ):
        return "Quarantine"
    if category == "incomplete_page" and normalized in {
        "MISSING_DELTA_ARTIFACT",
        "NON_BYTES_ARTIFACT",
        "INVALID_UTF8",
        "NON_CANONICAL_LINE_ENDINGS",
    }:
        return "Blocking"
    if category in {"unseen_overwrite", "index_inconsistency", "incomplete_page"}:
        return "Blocking"
    # A malformed path/reference is unsafe to publish unless it is one of the
    # explicitly quarantinable content evidence failures above.
    if category == "malformed_reference":
        return "Blocking"
    return "Advisory"


def is_publish_blocking(entry: "ErrorBookEntry") -> bool:
    """Return whether an open entry must prevent publication."""
    return entry.status == "open" and entry.error_class in PUBLISH_BLOCKING_CLASSES


def is_repair_candidate(entry: "ErrorBookEntry") -> bool:
    """Return whether an entry belongs in the bounded global repair loop."""
    return entry.status == "open" and entry.error_class in REPAIR_CLASSES

_MAX_TEXT = 2000
_MAX_PATH = 2000

# A stable error ID is ``error_`` followed by exactly 24 lowercase hex chars.
_ERROR_ID_RE = re.compile(r"error_[0-9a-f]{24}")

# Stable one-line system instruction for every structured model call.
_SYSTEM_INSTRUCTION = (
    "You are a rigorous wiki compiler. "
    "Return exactly the requested structured JSON object and nothing else."
)


# ──────────────────────────────────────────────
#  Shared validators
# ──────────────────────────────────────────────


def _require_non_blank(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-blank")
    return value


def _validate_text(value: str, field: str) -> str:
    _require_non_blank(value, field)
    if len(value) > _MAX_TEXT:
        raise ValueError(f"{field} exceeds {_MAX_TEXT} characters")
    return value


def _validate_error_id(value: str) -> str:
    if not isinstance(value, str) or not _ERROR_ID_RE.fullmatch(value):
        raise ValueError("error id must match error_[0-9a-f]{24}")
    return value


def _validate_safe_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("affected path must be a non-empty string")
    if len(value) > _MAX_PATH:
        raise ValueError(f"affected path exceeds {_MAX_PATH} characters")
    if value.startswith("/") or value.endswith("/"):
        raise ValueError("affected path must be relative without trailing slash")
    if "\\" in value:
        raise ValueError("affected path must not contain a backslash")
    if any(seg in ("", ".", "..") for seg in value.split("/")):
        raise ValueError("affected path must not contain empty or dot segments")
    return value


def _validate_affected_paths(values: List[str]) -> List[str]:
    for path in values:
        _validate_safe_path(path)
    if len(set(values)) != len(values):
        raise ValueError("affected paths must be unique")
    return values


# ──────────────────────────────────────────────
#  Discovery and attribution models
# ──────────────────────────────────────────────


class DiscoveredError(BaseModel):
    """One typed, stable discovery derived from a structural or content issue."""

    id: str
    category: str
    code: str
    phenomenon: str
    affected_paths: List[str] = Field(default_factory=list)
    affected_claim_ids: List[str] = Field(default_factory=list)
    model_backed: bool = False
    error_class: Literal["Blocking", "Quarantine", "Advisory", "Transient"] = "Blocking"

    model_config = ConfigDict(extra="forbid")
    @field_validator("id")
    def _id(cls, v: str) -> str:
        return _validate_error_id(v)

    @field_validator("code", "phenomenon")
    def _text(cls, v: str, field: "Field") -> str:
        return _validate_text(v, field.field_name)

    @field_validator("category")
    def _category(cls, v: str) -> str:
        if v not in ERROR_CATEGORIES:
            raise ValueError("category must be one of ERROR_CATEGORIES")
        return v

    @field_validator("error_class")
    def _error_class(cls, v: str) -> str:
        if v not in ERROR_CLASSES:
            raise ValueError("error_class must be one of ERROR_CLASSES")
        return v

    @field_validator("affected_paths")
    def _paths(cls, v: List[str]) -> List[str]:
        return _validate_affected_paths(v)


class ErrorAttribution(BaseModel):
    """Root cause, constraint and verification text for one new error."""

    error_id: str
    root_cause: str
    constraint: str
    verification: str

    model_config = ConfigDict(extra="forbid")
    @field_validator("error_id")
    def _id(cls, v: str) -> str:
        return _validate_error_id(v)

    @field_validator("root_cause", "constraint", "verification")
    def _text(cls, v: str, field: "Field") -> str:
        return _validate_text(v, field.field_name)


class ErrorAttributionBatch(BaseModel):
    """A bounded set of attributions, one per new discovery."""

    attributions: List[ErrorAttribution] = Field(..., min_length=1, max_length=100)

    model_config = ConfigDict(extra="forbid")
    @field_validator("attributions")
    def _unique_ids(cls, v: List[ErrorAttribution]) -> List[ErrorAttribution]:
        ids = [a.error_id for a in v]
        if len(set(ids)) != len(ids):
            raise ValueError("attribution error_ids must be unique")
        return v


# ──────────────────────────────────────────────
#  Error Book models
# ──────────────────────────────────────────────


class RepairAttemptRecord(BaseModel):
    """One durable repair attempt and the result of its verification gates."""

    batch_number: int
    accepted: bool
    resolution: str
    mutated_paths: List[str] = Field(default_factory=list)
    structural_issue_codes: List[str] = Field(default_factory=list)
    content_issue_codes: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
class ErrorBookEntry(BaseModel):
    """A persistent Error Book row for one discovered and attributed error."""

    id: str
    category: str
    code: str
    phenomenon: str
    # ``error_class`` was added after the first bundle schema.  The default is
    # intentionally Blocking so old bundles fail safe until reclassified by a
    # fresh discovery; new discoveries always set it deterministically.
    error_class: Literal["Blocking", "Quarantine", "Advisory", "Transient"] = "Blocking"
    root_cause: str
    constraint: str
    verification: str
    affected_paths: List[str] = Field(default_factory=list)
    first_seen_batch: int
    last_seen_batch: int
    status: Literal["open", "closed"] = "open"
    affected_claim_ids: List[str] = Field(default_factory=list)
    affected_source_ids: List[str] = Field(default_factory=list)
    affected_passage_ids: List[str] = Field(default_factory=list)
    rule_version: Optional[str] = None
    source_versions: Dict[str, str] = Field(default_factory=dict)
    # S4.3: the exact binding of a confirmed compile omission - the source range
    # it is about and the content version it was confirmed against.
    omission_range: List[int] = Field(default_factory=list)
    omission_source_sha256: str = ""
    omission_description: str = ""
    omission_page_sha256: str = ""
    omission_range_sha256: str = ""
    resolution: Optional[str] = None
    repair_history: List[RepairAttemptRecord] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
    @field_validator("id")
    def _id(cls, v: str) -> str:
        return _validate_error_id(v)

    @field_validator("code", "phenomenon", "root_cause", "constraint", "verification")
    def _text(cls, v: str, field: "Field") -> str:
        return _validate_text(v, field.field_name)

    @field_validator("category")
    def _category(cls, v: str) -> str:
        if v not in ERROR_CATEGORIES:
            raise ValueError("category must be one of ERROR_CATEGORIES")
        return v

    @field_validator("error_class")
    def _error_class(cls, v: str) -> str:
        if v not in ERROR_CLASSES:
            raise ValueError("error_class must be one of ERROR_CLASSES")
        return v

    @field_validator("affected_paths")
    def _paths(cls, v: List[str]) -> List[str]:
        return _validate_affected_paths(v)

    @field_validator("omission_range")
    def _omission_range(cls, v: List[int]) -> List[int]:
        if not v:
            return []
        if len(v) != 2 or any(isinstance(item, bool) or not isinstance(item, int)
                              for item in v):
            raise ValueError("omission_range must be [start, end]")
        if v[0] < 0 or v[1] <= v[0]:
            raise ValueError("omission_range must satisfy 0 <= start < end")
        return [int(v[0]), int(v[1])]

    @field_validator("omission_source_sha256", "omission_description")
    def _omission_text(cls, v: str) -> str:
        return _validate_text(v, "omission text") if v else ""

    @field_validator("first_seen_batch")
    def _first_batch(cls, v: int) -> int:
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ValueError("first_seen_batch must be >= 1")
        return v

    @model_validator(mode="after")
    def _batch_order(self):
        first = self.first_seen_batch
        last = self.last_seen_batch
        if first is not None and last is not None and last < first:
            raise ValueError("last_seen_batch must be >= first_seen_batch")
        return self


class ErrorBook(BaseModel):
    """An immutable set of Error Book entries, unique and sorted by ID."""

    entries: List[ErrorBookEntry] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
    @model_validator(mode="after")
    def _sort_and_dedupe(self):
        entries = list(getattr(self, "entries", []))
        ids = [e.id for e in entries]
        if len(set(ids)) != len(ids):
            raise ValueError("error book ids must be unique")
        entries.sort(key=lambda e: e.id)
        self.entries = entries
        return self


# ──────────────────────────────────────────────
#  Repair proposal and result models
# ──────────────────────────────────────────────


class ErrorRepairProposal(BaseModel):
    """A bounded repair proposal: resolved IDs plus constrained page mutations.

    Repair uses the exact ``CompileProposal`` claim/mutation rules but permits
    only ``update`` and ``delete`` operations.
    """

    resolved_error_ids: List[str] = Field(..., min_length=1, max_length=100)
    mutations: List[PageMutation] = Field(default_factory=list, max_length=10)
    claims: List[ClaimDraft] = Field(default_factory=list, max_length=100)

    model_config = ConfigDict(extra="forbid")
    @field_validator("resolved_error_ids")
    def _unique_resolved(cls, v: List[str]) -> List[str]:
        for item in v:
            _validate_error_id(item)
        if len(set(v)) != len(v):
            raise ValueError("resolved_error_ids must be unique")
        return v

    @field_validator("mutations")
    def _no_create_and_unique_paths(cls, v: List[PageMutation]) -> List[PageMutation]:
        for m in v:
            if m.operation == "create":
                raise ValueError("create is invalid in a repair proposal")
        paths = [m.path for m in v]
        if len(set(paths)) != len(paths):
            raise ValueError("mutation paths must be unique")
        return v

    @model_validator(mode="after")
    def _check_claims(self):
        mutations = getattr(self, "mutations", [])
        claims = getattr(self, "claims", [])
        target_paths = [
            m.path for m in mutations if m.operation in ("create", "update")
        ]
        target_set = set(target_paths)

        # Claim pages and per-target coverage are validated state-aware by
        # validate_claim_evidence(); a real LLM may update a page without a
        # matching claim (stage drift), so neither is fatal here.

        # Claim 出处（evidence.excerpt 精确出自 source passage）由
        # validate_claim_evidence() 统一把关；此处不再要求 claim.text 是页面
        # content 的子串（LLM 编译页面时会重写原文，该约束过严且先于 repair）。
        return self


class RepairRoundResult(BaseModel):
    """The auditable outcome of one bounded repair round."""

    state: CompilerState
    error_book: ErrorBook
    candidate_state: Optional[CompilerState] = None
    render_delta: Optional[object] = Field(default=None, exclude=True)
    render_index_delta: Optional[object] = Field(default=None, exclude=True)
    files: Dict[str, bytes] = Field(default_factory=dict)
    structural_report: StructuralReport
    content_report: ContentReport
    attempted_error_ids: List[str] = Field(default_factory=list)
    resolved_error_ids: List[str] = Field(default_factory=list)
    model_called: bool = False
    accepted: bool = False
    mutated_paths: List[str] = Field(default_factory=list, exclude=True)

    model_config = ConfigDict(extra="forbid")
class CompilerBatchResult(BaseModel):
    """The outcome of one Algorithm 1 batch plus Error Book lifecycle."""

    state: CompilerState
    error_book: ErrorBook
    algorithm1: Algorithm1BatchResult
    repair_round: Optional[RepairRoundResult] = None
    render_index_delta: Optional[object] = Field(default=None, exclude=True)
    mutated_paths: List[str] = Field(default_factory=list, exclude=True)

    model_config = ConfigDict(extra="forbid")
class FinalizationResult(BaseModel):
    """The outcome of exactly three sequential repair rounds."""

    state: CompilerState
    error_book: ErrorBook
    rounds: List[RepairRoundResult] = Field(..., min_length=3, max_length=3)
    files: Dict[str, bytes]
    structural_report: StructuralReport
    publishable: bool

    model_config = ConfigDict(extra="forbid")
# ──────────────────────────────────────────────
#  Request construction (AC-006 conventions)
# ──────────────────────────────────────────────


def _messages(payload: Dict) -> List[Dict]:
    """Build the two-message request for one structured model call."""
    content = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return [
        {"role": "system", "content": _SYSTEM_INSTRUCTION},
        {"role": "user", "content": content},
    ]


async def _call(
    model: ModelPort,
    payload: Dict,
    schema: Dict,
    max_tokens: int,
    *,
    model_name: Optional[str],
    tenant_id: str,
    context_id: str,
    trace_id: str,
    scene: str | None = None,
) -> Dict:
    return await model.complete(
        _messages(payload),
        schema,
        max_tokens=max_tokens,
        model=model_name,
        tenant_id=tenant_id,
        context_id=context_id,
        trace_id=trace_id,
        thinking={"type": "disabled"},
        scene=scene,
    )


# ──────────────────────────────────────────────
#  Discovery
# ──────────────────────────────────────────────


def _safe_paths(paths: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return sorted(out)


def _derive_error_id(
    category: str, code: str, phenomenon: str, affected_paths: List[str]
) -> str:
    payload = {
        "category": category,
        "code": code,
        "phenomenon": phenomenon,
        "affected_paths": affected_paths,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "error_" + hashlib.sha256(encoded).hexdigest()[:24]


def discover_errors(
    structural_report: StructuralReport,
    content_report: ContentReport,
) -> List[DiscoveredError]:
    """Convert every structural/content issue into one stable discovery.

    Pure: never mutates either report.  Returns discoveries sorted by ID and
    de-duplicated by identical ID.
    """
    by_id: Dict[str, DiscoveredError] = {}
    for issue in structural_report.issues:
        paths = [issue.path]
        if issue.target:
            paths.append(issue.target)
        paths = _safe_paths(paths)
        code = issue.code
        phenomenon = f"{code}: {issue.detail}"
        did = _derive_error_id(issue.category, code, phenomenon, paths)
        by_id[did] = DiscoveredError(
            id=did,
            category=issue.category,
            code=code,
            phenomenon=phenomenon,
            affected_paths=paths,
            error_class=classify_error_class(issue.category, code),
        )
    for issue in content_report.issues:
        paths = _safe_paths(list(issue.page_paths))
        code = issue.code
        phenomenon = f"{code}: {issue.detail}"
        did = _derive_error_id(issue.category, code, phenomenon, paths)
        by_id[did] = DiscoveredError(
            id=did,
            category=issue.category,
            code=code,
            phenomenon=phenomenon,
            affected_paths=paths,
            affected_claim_ids=list(issue.claim_ids),
            model_backed=issue.model_backed,
            error_class=classify_error_class(issue.category, code),
        )
    return [by_id[k] for k in sorted(by_id)]


# ──────────────────────────────────────────────
#  Error Book lifecycle
# ──────────────────────────────────────────────


def record_discoveries(
    book: ErrorBook,
    discoveries: List[DiscoveredError],
    attributions: List[ErrorAttribution],
    *,
    batch_number: int,
) -> ErrorBook:
    """Persist a batch of discoveries into a new Error Book.

    Requires ``batch_number >= 1`` and that ``attributions`` cover exactly the
    discovery IDs absent from the input book.  Never mutates any input.
    """
    if (
        not isinstance(batch_number, int)
        or isinstance(batch_number, bool)
        or batch_number < 1
    ):
        raise ValueError("batch_number must be >= 1")

    existing = {e.id: e for e in book.entries}

    # Reject duplicate attribution IDs before any ID mapping is built.
    attr_ids = [a.error_id for a in attributions]
    if len(set(attr_ids)) != len(attr_ids):
        raise ValueError("attribution error_ids must be unique")

    # De-duplicate identical repeats within one discovery list, but reject a
    # repeated ID whose category, code, phenomenon or affected paths differ.
    by_discovery_id: Dict[str, DiscoveredError] = {}
    for d in discoveries:
        if d.id in by_discovery_id:
            prev = by_discovery_id[d.id]
            if (
                prev.category != d.category
                or prev.code != d.code
                or prev.phenomenon != d.phenomenon
                or list(prev.affected_paths) != list(d.affected_paths)
                or prev.error_class != d.error_class
            ):
                raise ValueError(f"ERROR_ID_COLLISION: {d.id}")
            continue
        by_discovery_id[d.id] = d
    ordered_discoveries: List[DiscoveredError] = list(by_discovery_id.values())

    new_ids = [d.id for d in ordered_discoveries if d.id not in existing]
    attr_by_id = {a.error_id: a for a in attributions}
    if set(new_ids) != set(attr_by_id):
        raise ValueError(
            "attributions must cover exactly the new discovery ids"
        )

    new_entries: Dict[str, ErrorBookEntry] = dict(existing)
    for d in ordered_discoveries:
        if d.id in existing:
            entry = existing[d.id]
            if (
                entry.category != d.category
                or entry.code != d.code
                or entry.phenomenon != d.phenomenon
                or list(entry.affected_paths) != list(d.affected_paths)
                or entry.error_class != d.error_class
            ):
                raise ValueError(f"ERROR_ID_COLLISION: {d.id}")
            new_entries[d.id] = entry.model_copy(
                update={"last_seen_batch": batch_number, "status": "open"}
            )
        else:
            attr = attr_by_id[d.id]
            new_entries[d.id] = ErrorBookEntry(
                id=d.id,
                category=d.category,
                code=d.code,
                phenomenon=d.phenomenon,
                error_class=d.error_class,
                root_cause=attr.root_cause,
                constraint=attr.constraint,
                verification=attr.verification,
                affected_paths=list(d.affected_paths),
                affected_claim_ids=list(d.affected_claim_ids),
                rule_version="compiler-semantic-v1" if d.model_backed else "compiler-discovery-v1",
                first_seen_batch=batch_number,
                last_seen_batch=batch_number,
                status="open",
            )

    return ErrorBook(entries=[new_entries[k] for k in sorted(new_entries)])


def open_constraints(book: ErrorBook, limit: int = 12) -> List[str]:
    """Return a bounded set of reusable compile-time prevention rules.

    Blocking rules remain first.  One representative rule per Quarantine code
    is then injected so recurring grounding errors influence later batches
    without sending thousands of instance rows to every model call.  Advisory
    and Transient findings remain operational records, not prompt rules.
    """
    result: List[str] = []
    seen = set()
    eligible = [e for e in book.entries if e.status == "open" and e.error_class in {"Blocking", "Quarantine"}]
    eligible.sort(key=lambda e: (e.error_class != "Blocking", e.code, e.id))
    seen_codes = set()
    for entry in eligible:
        key = entry.constraint
        if key not in seen and (entry.error_class == "Blocking" or entry.code not in seen_codes):
            seen.add(key)
            seen_codes.add(entry.code)
            result.append(entry.constraint)
            if len(result) >= max(0, limit):
                break
    return result


# ──────────────────────────────────────────────
#  Repair target gate
# ──────────────────────────────────────────────


def _check_repair_targets(
    state: CompilerState, book: ErrorBook, proposal: ErrorRepairProposal
) -> StructuralReport:
    """Validate a repair proposal against the current state and Error Book.

    Every resolved ID must name an open entry; every mutation path must exist
    and be authorized by a resolved entry's ``affected_paths``; every resolved
    entry must have at least one affected page mutation.  These checks are
    combined with ``validate_update_targets`` on the exact mutation paths.
    """
    open_by_id = {e.id: e for e in book.entries if e.status == "open"}
    resolved = list(proposal.resolved_error_ids)
    resolved_set = set(resolved)
    mutation_paths = [m.path for m in proposal.mutations]
    mutation_set = set(mutation_paths)

    issues: List[StructuralIssue] = []

    for rid in resolved:
        if rid not in open_by_id:
            issues.append(
                StructuralIssue(
                    code="REPAIR_UNKNOWN_OPEN_ERROR",
                    category="malformed_reference",
                    path=rid,
                    target=None,
                    repairable=False,
                    detail=rid,
                )
            )

    for path in mutation_paths:
        if path not in state.pages:
            issues.append(
                StructuralIssue(
                    code="REPAIR_TARGET_MISSING",
                    category="unseen_overwrite",
                    path=path,
                    target=None,
                    repairable=False,
                    detail=path,
                )
            )
        elif not any(
            path in open_by_id[rid].affected_paths
            for rid in resolved_set
            if rid in open_by_id
        ):
            issues.append(
                StructuralIssue(
                    code="REPAIR_TARGET_NOT_AUTHORIZED",
                    category="unseen_overwrite",
                    path=path,
                    target=None,
                    repairable=False,
                    detail=path,
                )
            )

    for rid in resolved:
        entry = open_by_id.get(rid)
        if entry is not None:
            if not any(m.path in entry.affected_paths for m in proposal.mutations):
                issues.append(
                    StructuralIssue(
                        code="REPAIR_RESOLVED_NO_MUTATION",
                        category="malformed_reference",
                        path=rid,
                        target=None,
                        repairable=False,
                        detail=rid,
                    )
                )

    update_paths = [
        m.path for m in proposal.mutations if m.operation == "update"
    ]
    delete_paths = [
        m.path for m in proposal.mutations if m.operation == "delete"
    ]
    target_report = validate_update_targets(
        state,
        selected_existing_paths=mutation_paths,
        create_paths=[],
        update_paths=update_paths,
        delete_paths=delete_paths,
    )
    issues.extend(target_report.issues)
    return StructuralReport(issues=issues)


def _close_errors(book: ErrorBook, resolved_ids: List[str]) -> ErrorBook:
    entries = []
    resolved_set = set(resolved_ids)
    for entry in book.entries:
        if entry.id in resolved_set:
            entry = entry.model_copy(update={"status": "closed"})
        entries.append(entry)
    return ErrorBook(entries=entries)


def _record_repair_attempt(
    book: ErrorBook,
    error_ids: List[str],
    *,
    batch_number: int,
    accepted: bool,
    resolution: str,
    mutated_paths: List[str],
    structural_report: StructuralReport,
    content_report: ContentReport,
) -> ErrorBook:
    """Append a bounded audit record to every error the model tried to fix."""
    attempted = set(error_ids)
    record = RepairAttemptRecord(
        batch_number=batch_number,
        accepted=accepted,
        resolution=resolution,
        mutated_paths=sorted(set(mutated_paths)),
        structural_issue_codes=sorted({issue.code for issue in structural_report.issues}),
        content_issue_codes=sorted({issue.code for issue in content_report.issues}),
    )
    entries: List[ErrorBookEntry] = []
    for entry in book.entries:
        if entry.id in attempted:
            # Retain recent evidence without allowing a long-lived Error Book row
            # to grow without bound.
            history = [*entry.repair_history, record][-20:]
            entry = entry.model_copy(update={"repair_history": history})
        entries.append(entry)
    return ErrorBook(entries=entries)


def _overlay_with_delta(
    base: WikiRenderIndex | WikiRenderIndexOverlay,
    delta: Optional[RenderIndexDelta],
) -> WikiRenderIndexOverlay:
    """Apply a small batch delta to a shared index without copying it."""
    overlay = base.fork() if isinstance(base, WikiRenderIndexOverlay) else base.overlay()
    if delta is not None:
        for path, edges in delta.page_edges:
            overlay.set_page(path, edges)
    return overlay


# ──────────────────────────────────────────────
#  Bounded repair round
# ──────────────────────────────────────────────


async def run_error_repair_round(
    state: CompilerState,
    book: ErrorBook,
    model: ModelPort,
    *,
    tenant_id: str = "",
    context_id: str = "",
    trace_id: str = "",
    model_name: Optional[str] = None,
    render_index: Optional[WikiRenderIndex | WikiRenderIndexOverlay] = None,
) -> RepairRoundResult:
    """Run one bounded repair round over the current state and Error Book.

    Returns a fresh result and never mutates ``state`` or ``book``.  A rejected
    round leaves the returned state unchanged and records the failed verification
    in the returned Error Book; the applied candidate remains available for audit.
    """
    state = state.model_copy(deep=True)
    book = book.model_copy(deep=True)

    open_entries = sorted(
        (e for e in book.entries if is_repair_candidate(e)), key=lambda e: e.id
    )

    # 2. No open entries -> no model call.
    if not open_entries:
        # A clean repair round is a no-op.  The finalization caller owns the
        # single complete render immediately before publication; rendering
        # here would make a clean three-round finalization render four trees.
        content_report = merge_semantic_review(
            ContentReport(), SemanticReview(reviewed_claim_ids=[])
        )
        return RepairRoundResult(
            state=state,
            error_book=book,
            candidate_state=None,
            files={},
            structural_report=StructuralReport(issues=[]),
            content_report=content_report,
            attempted_error_ids=[],
            resolved_error_ids=[],
            model_called=False,
            accepted=True,
        )

    # 3. One repair_error_book call with open errors (entry-ID order) and only
    #    affected paths present in state.pages.  Shared paths are de-duplicated
    #    and the result is sorted by path: exactly one snapshot per affected
    #    path.
    affected = sorted(
        {
            p
            for e in open_entries
            for p in e.affected_paths
            if p in state.pages
        }
    )
    payload = {
        "stage": "repair_error_book",
        "batch_number": state.batch_number,
        "open_errors": [e.model_dump() for e in open_entries],
        "page_snapshots": [state.pages[p].model_dump() for p in affected],
    }
    raw = await _call(
        model,
        payload,
        ErrorRepairProposal.model_json_schema(),
        8192,
        model_name=model_name,
        tenant_id=tenant_id,
        context_id=context_id,
        trace_id=trace_id,
        scene="jonex_wiki_compile_repair",
    )
    proposal = ErrorRepairProposal.model_validate(raw)
    attempted = list(proposal.resolved_error_ids)

    # 4. Target gate: authorization + update-target validation.
    target_report = _check_repair_targets(state, book, proposal)
    if not target_report.is_valid:
        # The candidate was never applied; target-gate failure should not
        # trigger a full-tree render just to populate an audit field.
        result_book = _record_repair_attempt(
            book,
            attempted,
            batch_number=state.batch_number,
            accepted=False,
            resolution="target_validation_rejected",
            mutated_paths=[m.path for m in proposal.mutations],
            structural_report=target_report,
            content_report=ContentReport(),
        )
        return RepairRoundResult(
            state=state,
            error_book=result_book,
            candidate_state=None,
            files={},
            structural_report=target_report,
            content_report=ContentReport(),
            attempted_error_ids=attempted,
            resolved_error_ids=[],
            model_called=True,
            accepted=False,
        )

    # 5. Apply mutations in caller order to a deep copy; render, code-repair,
    #    revalidate structurally, and validate the proposal claims.
    candidate_state = state.model_copy(deep=True)
    for m in proposal.mutations:
        if m.operation == "delete":
            candidate_state.pages.pop(m.path, None)
            candidate_state.index.pop(m.path, None)
        else:
            candidate_state.pages[m.path] = m.page
            candidate_state.index[m.path] = m.page.index

    base_index = render_index or WikiRenderIndex.from_state(state)
    impact, candidate_index = plan_mutation_impact(
        state,
        candidate_state,
        proposal.mutations,
        base_index,
    )
    candidate_delta = render_wiki_delta(
        candidate_state,
        impact,
        candidate_index,
    )
    structural_report = validate_wiki_delta(
        candidate_state,
        candidate_delta,
        impact,
        candidate_index,
    )
    content_report = validate_claim_evidence(candidate_state, proposal.claims)

    # 6. Bounded semantic review when both deterministic reports are valid and
    #    there is at least one validated claim; delete-only completes locally.
    structural_ok = structural_report.is_valid
    evidence_ok = content_report.is_valid
    if structural_ok and evidence_ok and content_report.validated_claims:
        mutated_paths = sorted(
            m.path
            for m in proposal.mutations
            if m.operation == "update" and m.path in candidate_state.pages
        )
        sem_payload = {
            "stage": "repair_semantic_review",
            "resolved_error_ids": list(attempted),
            "claims": [c.model_dump() for c in content_report.validated_claims],
            "page_snapshots": [
                candidate_state.pages[p].model_dump() for p in mutated_paths
            ],
        }
        raw = await _call(
            model,
            sem_payload,
            SemanticReview.model_json_schema(),
            4096,
            model_name=model_name,
            tenant_id=tenant_id,
            context_id=context_id,
            trace_id=trace_id,
            scene="jonex_wiki_compile_review",
        )
        review = SemanticReview.model_validate(raw)
        content_report = merge_semantic_review(content_report, review)
    elif structural_ok and evidence_ok and not content_report.validated_claims:
        content_report = merge_semantic_review(
            content_report, SemanticReview(reviewed_claim_ids=[])
        )

    # 7. Accept only when structural validation is valid and the final content
    #    report is publishable; only then close the resolved IDs and recompute
    #    the active constraints from the remaining open entries.  A rejected
    #    round keeps the returned state unchanged, appends an audit record to the
    #    Error Book, and returns no candidate files.
    accepted = structural_report.is_valid and content_report.publishable
    resolved = []
    audited_book = _record_repair_attempt(
        book,
        attempted,
        batch_number=state.batch_number,
        accepted=accepted,
        resolution=("verified_and_closed" if accepted else "verification_rejected"),
        mutated_paths=[m.path for m in proposal.mutations],
        structural_report=structural_report,
        content_report=content_report,
    )
    if accepted:
        resolved = list(attempted)
        result_book = _close_errors(audited_book, resolved)
        candidate_state.active_constraints = open_constraints(result_book)
        result_state = candidate_state
        result_files = candidate_delta.files
        result_delta = candidate_delta
        result_index_delta = candidate_index.to_delta()
    else:
        result_state = state
        result_book = audited_book
        result_files = {}
        result_delta = None
        result_index_delta = None

    return RepairRoundResult(
        state=result_state,
        error_book=result_book,
        candidate_state=candidate_state,
        files=result_files,
        render_delta=result_delta,
        render_index_delta=result_index_delta,
        structural_report=structural_report,
        content_report=content_report,
        attempted_error_ids=attempted,
        resolved_error_ids=resolved,
        model_called=True,
        accepted=accepted,
        mutated_paths=(
            sorted(m.path for m in proposal.mutations) if accepted else []
        ),
    )


# ──────────────────────────────────────────────
#  Batch orchestration
# ──────────────────────────────────────────────


async def run_compiler_batch(
    state: CompilerState,
    book: ErrorBook,
    passage_ids: List[str],
    model: ModelPort,
    *,
    tenant_id: str = "",
    context_id: str = "",
    trace_id: str = "",
    model_name: Optional[str] = None,
    repair_interval: int = 10,
    defer_repair: bool = False,
    render_index: Optional[WikiRenderIndex | WikiRenderIndexOverlay] = None,
    compile_main_prompt: str = "",
    retry_constraints: Sequence[str] = (),
) -> CompilerBatchResult:
    """Run one Algorithm 1 batch and persist/repair Error Book discoveries.

    Raises ``ValueError`` for an out-of-range ``repair_interval`` before any
    model call.  The wrapper returns the incremented state even when Algorithm 1
    halted; rejected proposal changes remain absent exactly as AC-006 specifies.

    ``defer_repair`` suppresses this batch's bounded repair round so a caller
    running parallel batches can merge them first and run a single repair round
    at the merge boundary (see ``production.merge_parallel_batches``).
    """
    if (
        not isinstance(repair_interval, int)
        or isinstance(repair_interval, bool)
        or not (1 <= repair_interval <= 1000)
    ):
        raise ValueError("repair_interval must be between 1 and 1000")

    # 1. Detach mutable maps/lists and share the immutable source archive and
    # page snapshots.  A full deep copy here was repeated once per passage
    # worker and exhausted the 8 GiB cgroup on full corpora.
    working = _copy_state_for_mutation(state)
    working.active_constraints = list(dict.fromkeys([
        *open_constraints(book), *retry_constraints,
    ]))
    base_render_index = render_index or WikiRenderIndex.from_state(state)

    # 2. Run Algorithm 1 once with the caller's passage IDs and routing values.
    algorithm1 = await run_algorithm1_batch(
        working,
        passage_ids,
        model,
        tenant_id=tenant_id,
        context_id=context_id,
        trace_id=trace_id,
        model_name=model_name,
        render_index=base_render_index,
        compile_main_prompt=compile_main_prompt,
    )
    batch_render_index = _overlay_with_delta(
        base_render_index,
        algorithm1.render_index_delta,
    )

    # 3. Increment batch once and discover issues from every returned passage.
    result_state = _copy_state_for_mutation(algorithm1.state)
    result_state.batch_number += 1
    new_batch = result_state.batch_number

    discoveries: List[DiscoveredError] = []
    seen_ids = set()
    for pc in algorithm1.passages:
        for d in discover_errors(pc.structural_report, pc.content_report):
            if d.id not in seen_ids:
                seen_ids.add(d.id)
                discoveries.append(d)

    # 4. Attribute only discovery IDs absent from the input book.
    existing_ids = {e.id for e in book.entries}
    new_discoveries = [d for d in discoveries if d.id not in existing_ids]
    attributions: List[ErrorAttribution] = []
    if new_discoveries:
        # The detector already supplies a typed category, object and reason.
        # Preserve that finding; a second model call cannot prove its cause.
        attributions = [ErrorAttribution(error_id=d.id,
            root_cause="Detector finding (causal diagnosis not yet proven): " + d.phenomenon[:1500],
            constraint="Resolve " + d.code + " on its affected objects without discarding source evidence.",
            verification="Rerun the same structural/evidence detector after the local repair.")
            for d in new_discoveries]

    # 5. Record discoveries, refresh constraints, and run one repair round only
    #    when the new batch number is a multiple of the interval and open.
    new_book = record_discoveries(
        book, discoveries, attributions, batch_number=new_batch
    )
    result_state.active_constraints = open_constraints(new_book)

    result_book = new_book
    repair_round = None
    if not defer_repair and new_batch % repair_interval == 0 and any(
        is_repair_candidate(e) for e in new_book.entries
    ):
        repair_round = await run_error_repair_round(
            result_state,
            new_book,
            model,
            tenant_id=tenant_id,
            context_id=context_id,
            trace_id=trace_id,
            model_name=model_name,
            render_index=batch_render_index,
        )
        # A repair round (accepted or rejected) owns the returned state/book.
        result_state = repair_round.state
        result_book = repair_round.error_book

    result_index_delta = algorithm1.render_index_delta
    mutated_paths: List[str] = []
    for passage_result in algorithm1.passages:
        if passage_result.accepted:
            mutated_paths.extend(m.path for m in passage_result.proposal.mutations)
    if repair_round is not None and repair_round.accepted:
        # A semantically clean repair may close an entry without changing a
        # page; retain the Algorithm-1 delta in that case.
        if repair_round.render_index_delta is not None:
            result_index_delta = repair_round.render_index_delta
        mutated_paths.extend(repair_round.mutated_paths)

    return CompilerBatchResult(
        state=result_state,
        error_book=result_book,
        algorithm1=algorithm1,
        repair_round=repair_round,
        render_index_delta=result_index_delta,
        mutated_paths=sorted(set(mutated_paths)),
    )


# ──────────────────────────────────────────────
#  Three-round finalization
# ──────────────────────────────────────────────


async def finalize_error_book(
    state: CompilerState,
    book: ErrorBook,
    model: ModelPort,
    *,
    tenant_id: str = "",
    context_id: str = "",
    trace_id: str = "",
    model_name: Optional[str] = None,
    render_index: Optional[WikiRenderIndex] = None,
) -> FinalizationResult:
    """Run exactly three sequential repair rounds and report publishability.

    Rounds with no open errors make no model call (see
    ``run_error_repair_round``), so clean compiles already stop paying here.
    """
    cur_state = state.model_copy(deep=True)
    cur_book = book.model_copy(deep=True)
    render_index = render_index or WikiRenderIndex.from_state(cur_state)
    rounds: List[RepairRoundResult] = []

    for _ in range(3):
        round_result = await run_error_repair_round(
            cur_state,
            cur_book,
            model,
            render_index=render_index,
            tenant_id=tenant_id,
            context_id=context_id,
            trace_id=trace_id,
            model_name=model_name,
        )
        rounds.append(round_result)
        # Rejected mutations roll back state, not the audit of the attempt.
        cur_book = round_result.error_book
        if round_result.accepted:
            cur_state = round_result.state
            if round_result.render_index_delta is not None:
                render_index.apply(round_result.render_index_delta)

    # The final publication boundary owns exactly one complete render; the
    # validator consumes those bytes instead of regenerating the full tree.
    def render_and_validate():
        files = render_wiki_tree(cur_state, index=render_index)
        structural_report = validate_wiki_tree(
            cur_state,
            files,
            index=render_index,
            expected_files=files,
        )
        return files, structural_report

    files, structural_report = await asyncio.to_thread(render_and_validate)
    # Only Blocking entries prevent publication.  Quarantine entries are kept
    # out of the query-facing corpus, Advisory entries are telemetry, and
    # Transient entries are retried at their local call site; none of these
    # should force a three-round global repair of an otherwise valid wiki.
    publishable = structural_report.is_valid and not any(
        is_publish_blocking(e) for e in cur_book.entries
    )

    return FinalizationResult(
        state=cur_state,
        error_book=cur_book,
        rounds=rounds,
        files=files,
        structural_report=structural_report,
        publishable=publishable,
    )


__all__ = [
    "ERROR_CATEGORIES",
    "ERROR_CLASSES",
    "PUBLISH_BLOCKING_CLASSES",
    "REPAIR_CLASSES",
    "classify_error_class",
    "is_publish_blocking",
    "is_repair_candidate",
    "DiscoveredError",
    "ErrorAttribution",
    "ErrorAttributionBatch",
    "ErrorBookEntry",
    "ErrorBook",
    "ErrorRepairProposal",
    "RepairRoundResult",
    "CompilerBatchResult",
    "FinalizationResult",
    "discover_errors",
    "record_discoveries",
    "open_constraints",
    "run_error_repair_round",
    "run_compiler_batch",
    "finalize_error_book",
]
