"""Shared compile DTOs.

Extracted verbatim from ``原引擎/algorithm1.py``: the public
model contracts used by candidate selection, mutation planning and batch
acceptance.  The old import path stays available through the re-export shim;
public field names and validation semantics are unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import ConfigDict, BaseModel, Field, model_validator, field_validator

from jonex_wiki.domain.engine.models.compiler_state import (
    MAX_SOURCE_LINK_TARGETS,
    CompilerState,
    PageIndexEntry,
    PageSelection,
    WikiPageSnapshot,
    LinkMention,
    normalize_wiki_path,
)
from jonex_wiki.domain.engine.compile.content_validation import (
    ClaimDraft,
    ContentReport,
)
from jonex_wiki.domain.engine.compile.structural import StructuralReport



# ──────────────────────────────────────────────
#  Public model contracts
# ──────────────────────────────────────────────


class PageMutation(BaseModel):
    """One constrained create/update/delete operation on a Wiki page."""

    operation: Literal["create", "update", "delete", "facts"]
    path: str
    page: Optional[WikiPageSnapshot] = None
    link_mentions: List[LinkMention] = Field(default_factory=list)

    @field_validator("link_mentions", mode="before")
    def _readable_links(cls, value):
        links = []
        for item in value if isinstance(value, list) else ():
            try:
                link = LinkMention.model_validate(item)
            except (ValueError, TypeError):
                continue
            if link.target_mention.strip():
                links.append(link)
        return links

    model_config = ConfigDict(extra="forbid")

    @field_validator("path")
    def _canonical(cls, v: str) -> str:
        return normalize_wiki_path(v)

    @model_validator(mode="after")
    def _check_operation(self):
        operation = self.operation
        page = self.page
        path = self.path
        if operation in ("create", "update"):
            if page is None:
                raise ValueError("create/update require a page")
            if page.index.path != path:
                raise ValueError("page.index.path must equal mutation path")
        elif operation in ("delete", "facts"):
            if page is not None:
                raise ValueError("delete/facts require page to be None")
        return self


class CompileProposal(BaseModel):
    """A bounded compilation proposal: mutations plus grounded claims.

    ``mutations`` may be empty: a passage whose content warrants no page change
    (e.g. an update candidate the model judges already current) completes with
    no mutations instead of failing closed.
    """

    mutations: List[PageMutation] = Field(default_factory=list, max_length=10)
    claims: List[ClaimDraft] = Field(default_factory=list, max_length=100)

    model_config = ConfigDict(extra="forbid")

    @field_validator("mutations")
    def _unique_mutation_paths(cls, v: List[PageMutation]) -> List[PageMutation]:
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
        # validate_claim_evidence(); a real LLM may create a page without
        # emitting a matching claim (stage drift), so neither is fatal here.

        # Claim 出处（evidence.excerpt 精确出自 source passage）由
        # validate_claim_evidence() 统一把关；此处不再要求 claim.text 是页面
        # content 的子串（LLM 编译页面时会重写原文，该约束过严且先于 repair）。
        return self


class CombinedCompileResponse(BaseModel):
    """One-call output of page selection plus the compile proposal.

    Collapses SELECTPAGES + COMPILEWIKIPAGES into a single model call per
    passage (2 → 1).  ``selection`` and ``proposal`` use their existing
    schemas; the deterministic target / structural / evidence gates below
    still run unchanged.
    """

    selection: PageSelection = Field(default_factory=PageSelection)
    proposal: CompileProposal = Field(default_factory=CompileProposal)

    model_config = ConfigDict(extra="ignore")


class NoClaimDecision(BaseModel):
    """Failure-only triage deciding whether an empty compile merits retry."""

    action: Literal["accept_source_only", "retry_claim_compilation"]
    classification: Literal[
        "legitimate_no_claim", "duplicate_covered", "model_protocol_failure"
    ]
    reason: str

    model_config = ConfigDict(extra="forbid")

    @field_validator("reason")
    def _bounded_reason(cls, value: str) -> str:
        normalized = " ".join((value or "").split())
        if not normalized:
            raise ValueError("no-claim decision reason must be non-blank")
        return normalized[:240]

    @model_validator(mode="after")
    def _action_matches_classification(self):
        action = self.action
        classification = self.classification
        if action == "accept_source_only" and classification == "model_protocol_failure":
            raise ValueError("model protocol failure requires retry")
        if action == "retry_claim_compilation" and classification != "model_protocol_failure":
            raise ValueError("only a repairable model failure may request retry")
        return self


class PassageCompileResult(BaseModel):
    """The auditable outcome of compiling a single passage."""

    passage_id: str
    candidates: List[PageIndexEntry]
    selection: PageSelection
    proposal: CompileProposal
    candidate_state: Optional[CompilerState] = None
    # Renderer-owned output is intentionally incremental.  ``files`` is kept
    # as a compatibility field and contains only delta writes, never a full
    # tree.  The runtime objects are excluded from serialized audit payloads.
    render_delta: Optional[Any] = Field(default=None, exclude=True)
    render_index_delta: Optional[Any] = Field(default=None, exclude=True)
    files: Dict[str, bytes] = Field(default_factory=dict)
    structural_report: StructuralReport
    content_report: ContentReport
    accepted: bool

    model_config = ConfigDict(extra="forbid")


class Algorithm1BatchResult(BaseModel):
    """The final outcome of processing an ordered batch of passages."""

    state: CompilerState
    passages: List[PassageCompileResult] = Field(default_factory=list)
    render_index_delta: Optional[Any] = Field(default=None, exclude=True)
    halted_at_passage_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")
