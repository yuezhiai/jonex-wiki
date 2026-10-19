"""Pure structural validation and deterministic repair for a Wiki tree.

This module is the read-only gate used before a proposed Wiki state can be
published.  It validates the deterministic tree produced by AC-003
(``render_wiki_tree``), emits typed stable issues, prevents updates to unseen
pages and can rebuild renderer-owned artifacts from an accepted
``CompilerState``.

Paper-required behavior fixed here:

- structural validation occurs before applying/publishing an update;
- paths, required page structure, references, links, indices and reciprocal
  backlinks are checked;
- an update cannot overwrite an existing page outside selected set ``S``;
- deterministic code repair does not falsely close unresolved errors.

All functions are pure: they never call a model, touch storage, the
filesystem, a database, a clock, a random source or the network, and never
mutate ``state`` or the caller's collections.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict

from jonex_wiki.domain.engine.models.compiler_state import CompilerState, canonical_wiki_path
from jonex_wiki.domain.engine.compile.incremental import (
    MutationImpact,
    WikiRenderDelta,
    WikiRenderIndex,
    WikiRenderIndexOverlay,
)
from jonex_wiki.domain.engine.compile.rendering import render_wiki_delta, render_wiki_tree

__all__ = [
    "MutationImpact",
    "StructuralIssue",
    "StructuralReport",
    "validate_update_targets",
    "validate_wiki_tree",
    "validate_wiki_delta",
    "repair_wiki_tree",
]

IssueCategory = Literal[
    "dangling_link",
    "incomplete_page",
    "malformed_reference",
    "unseen_overwrite",
    "index_inconsistency",
]

# A complete WikiLink token is exactly ``[[target|non-empty-label]]`` where
# target carries no brackets or pipe and the label is non-empty without
# brackets.  Anything that remains after removing these tokens is malformed.
_TOKEN_RE = re.compile(r"\[\[(?P<target>[^\]\[|]+)\|(?P<label>[^\]\[]+)\]\]")

# Fenced code blocks are not wiki-link source: docs that embed source code may
# legitimately contain '[[', ']]' (e.g. Python lists) and must not be flagged.
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)

_GLOBAL_INDEX_ARTIFACT = "wiki/index.md"


class StructuralIssue(BaseModel):
    """One typed, stable structural issue."""

    code: str = Field(...)
    category: IssueCategory = Field(...)
    path: str = Field(...)
    target: Optional[str] = None
    repairable: bool = False
    detail: str = Field(...)

    model_config = ConfigDict(extra="forbid")
    @field_validator("code", "path", "detail")
    def _non_blank(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be non-blank")
        return v


class StructuralReport(BaseModel):
    """A sorted, de-duplicated set of structural issues."""

    issues: List[StructuralIssue] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
    @property
    def is_valid(self) -> bool:
        return not self.issues

    @property
    def fatal_count(self) -> int:
        return sum(1 for i in self.issues if not i.repairable)

    @property
    def repairable_count(self) -> int:
        return sum(1 for i in self.issues if i.repairable)

    @model_validator(mode="after")
    def _sort_and_dedupe(self):
        issues = list(getattr(self, "issues", []))
        seen = set()
        result: List[StructuralIssue] = []
        for issue in issues:
            key = (issue.code, issue.path, issue.target or "", issue.detail)
            if key not in seen:
                seen.add(key)
                result.append(issue)
        result.sort(key=lambda i: (i.code, i.path, i.target or "", i.detail))
        self.issues = result
        return self


# ──────────────────────────────────────────────
#  Update-target gate
# ──────────────────────────────────────────────


def validate_update_targets(
    state: CompilerState,
    *,
    selected_existing_paths: List[str],
    create_paths: List[str],
    update_paths: List[str],
    delete_paths: List[str],
) -> StructuralReport:
    """Validate an update against the current state before it is applied.

    Pure.  Applies the unseen-overwrite and missing-target rules without
    inferring, dropping or adding a selection.  Every emitted issue is
    non-repairable.
    """
    issues: List[StructuralIssue] = []

    def _target_issue(code: str, path: str) -> StructuralIssue:
        return StructuralIssue(
            code=code,
            category="unseen_overwrite",
            path=path,
            target=None,
            repairable=False,
            detail=path,
        )

    def _malformed_issue(path: str) -> StructuralIssue:
        return StructuralIssue(
            code="NON_CANONICAL_UPDATE_PATH",
            category="malformed_reference",
            path=path,
            target=None,
            repairable=False,
            detail=path,
        )

    valid: Dict[str, List[str]] = {"selected": [], "create": [], "update": [], "delete": []}
    for name, paths in (
        ("selected", selected_existing_paths),
        ("create", create_paths),
        ("update", update_paths),
        ("delete", delete_paths),
    ):
        for path in paths:
            try:
                canonical_wiki_path(path)
            except ValueError:
                issues.append(_malformed_issue(path))
            else:
                valid[name].append(path)

    # Duplicate rules run on the original caller lists, not on the canonical
    # subset, so a repeated non-canonical path is still flagged.
    # A repeated selected path emits DUPLICATE_SELECTED_PATH.
    seen_selected = set()
    for path in selected_existing_paths:
        if path in seen_selected:
            issues.append(_target_issue("DUPLICATE_SELECTED_PATH", path))
        seen_selected.add(path)

    # A target repeated within one action or present in more than one action
    # emits one DUPLICATE_UPDATE_TARGET for that path.
    action_paths = create_paths + update_paths + delete_paths
    for path, count in Counter(action_paths).items():
        if count > 1:
            issues.append(_target_issue("DUPLICATE_UPDATE_TARGET", path))

    # A selected path absent from state.pages.
    for path in valid["selected"]:
        if path not in state.pages:
            issues.append(_target_issue("MISSING_SELECTED_PAGE", path))

    # A create path already present in state.pages.
    for path in valid["create"]:
        if path in state.pages:
            issues.append(_target_issue("UNSEEN_OVERWRITE", path))

    # An update/delete path absent from state.pages.
    selected_set = set(valid["selected"])
    for path in valid["update"] + valid["delete"]:
        if path not in state.pages:
            issues.append(_target_issue("MISSING_UPDATE_TARGET", path))
        elif path not in selected_set:
            issues.append(_target_issue("UNSEEN_OVERWRITE", path))

    return StructuralReport(issues=issues)


# ──────────────────────────────────────────────
#  Wiki-tree gate
# ──────────────────────────────────────────────


def _is_index_artifact(key: str) -> bool:
    return key == _GLOBAL_INDEX_ARTIFACT or key.endswith("/index.md")


def _is_safe_artifact_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    if not key.startswith("wiki/"):
        return False
    if not key.endswith(".md"):
        return False
    if "\\" in key:
        return False
    parts = key.split("/")
    return not any(p == "" or p in (".", "..") for p in parts)


def _is_valid_target(target: str, expected_artifacts: Optional[set]) -> bool:
    if target == "index.md":
        return True
    if target.endswith("/index.md"):
        return (
            True
            if expected_artifacts is None
            else ("wiki/" + target) in expected_artifacts
        )
    try:
        canonical_wiki_path(target)
    except ValueError:
        return False
    return True


def _scan_links(
    state: CompilerState,
    expected_artifacts: Optional[set],
    source_path: str,
    text: str,
    index: Optional[WikiRenderIndex | WikiRenderIndexOverlay] = None,
) -> List[StructuralIssue]:
    # Model-generated wiki links are frequently malformed, dangling or
    # self-referential under a real LLM.  Failing the whole compile on them is
    # too brittle; degrade link-quality problems to warnings and let the page
    # content checks (missing/unexpected artifacts, UTF-8, line endings) remain
    # the fatal gate.
    text = _CODE_FENCE_RE.sub(" ", text)
    for match in _TOKEN_RE.finditer(text):
        target = match.group("target")
        if not _is_valid_target(target, expected_artifacts):
            logger.warning(
                "wiki link invalid target | page=%s target=%r", source_path, target
            )
            continue
        artifact = "wiki/" + target
        if expected_artifacts is None:
            if target == "index.md":
                artifact_exists = True
            elif target.endswith("/index.md"):
                directory = target[: -len("/index.md")]
                artifact_exists = index is not None and index.has_directory(directory)
            else:
                artifact_exists = target in state.pages
        else:
            artifact_exists = artifact in expected_artifacts
        if not artifact_exists:
            logger.warning(
                "wiki link dangling | page=%s target=%r", source_path, target
            )
        elif source_path in state.pages and target == source_path:
            logger.warning(
                "wiki link self | page=%s target=%r", source_path, target
            )
    remainder = _TOKEN_RE.sub("", text)
    if "[[" in remainder:
        logger.warning("wiki link stray [[ | page=%s", source_path)
    if "]]" in remainder:
        logger.warning("wiki link stray ]] | page=%s", source_path)
    return []


def _validate_structured_links(state: CompilerState) -> List[StructuralIssue]:
    """Validate cheap link metadata across the complete candidate snapshot."""
    issues: List[StructuralIssue] = []
    for page_path, snapshot in state.pages.items():
        for link in snapshot.links:
            if link.target_id not in state.pages:
                issues.append(StructuralIssue(
                    code="DANGLING_STRUCTURED_LINK", category="dangling_link",
                    path="wiki/" + page_path, target=link.target_id,
                    repairable=False, detail=link.why_visit,
                ))
            elif link.target_id == page_path:
                issues.append(StructuralIssue(
                    code="SELF_STRUCTURED_LINK", category="malformed_reference",
                    path="wiki/" + page_path, target=link.target_id,
                    repairable=False, detail=link.why_visit,
                ))
    return issues


def validate_wiki_tree(
    state: CompilerState,
    files: Dict[str, bytes],
    *,
    index: Optional[WikiRenderIndex | WikiRenderIndexOverlay] = None,
    expected_files: Optional[Dict[str, bytes]] = None,
) -> StructuralReport:
    """Validate a candidate artifact mapping against the canonical tree.

    Read-only.  Compares against ``render_wiki_tree(state)`` byte-for-byte,
    checks artifact-path safety, required structure, strict UTF-8 line
    endings and scans complete WikiLink tokens for malformed, dangling and
    self references (also inspecting ``state.pages[*].related_pages``).
    """
    expected = (
        expected_files
        if expected_files is not None
        else render_wiki_tree(state, index=index)
    )
    expected_keys = set(expected)

    issues: List[StructuralIssue] = []

    # Missing expected artifacts are reported first, independently of anything
    # the caller supplied in ``files``.
    for key in expected:
        if key not in files:
            category: IssueCategory = (
                "index_inconsistency" if _is_index_artifact(key) else "incomplete_page"
            )
            issues.append(
                StructuralIssue(
                    code="MISSING_ARTIFACT",
                    category=category,
                    path=key,
                    target=None,
                    repairable=True,
                    detail=key,
                )
            )

    # Every actual file entry is pushed through all independent checks: path
    # safety / expectedness, value type, strict UTF-8, line endings, expected
    # content comparison and WikiLink scanning.  Only a non-``bytes`` value or
    # a strict-UTF-8 decode failure may skip the text-level checks.  Path
    # safety or unexpectedness never short-circuits these.
    for key, value in files.items():
        if _is_safe_artifact_key(key):
            is_expected = key in expected_keys
            if not is_expected:
                issues.append(
                    StructuralIssue(
                        code="UNEXPECTED_ARTIFACT",
                        category="index_inconsistency",
                        path=key,
                        target=None,
                        repairable=True,
                        detail=key,
                    )
                )
        else:
            is_expected = False
            issues.append(
                StructuralIssue(
                    code="UNSAFE_ARTIFACT_PATH",
                    category="malformed_reference",
                    path=key,
                    target=None,
                    repairable=True,
                    detail=key,
                )
            )

        if not isinstance(value, bytes):
            issues.append(
                StructuralIssue(
                    code="NON_BYTES_ARTIFACT",
                    category="incomplete_page",
                    path=key,
                    target=None,
                    repairable=True,
                    detail=key,
                )
            )
            # No text-level checks (line endings, content, links) on non-bytes.
            continue

        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            issues.append(
                StructuralIssue(
                    code="INVALID_UTF8",
                    category="incomplete_page",
                    path=key,
                    target=None,
                    repairable=True,
                    detail=key,
                )
            )
            # No text-level checks on bytes that fail strict UTF-8 decoding.
            continue

        if "\r" in text or not value.endswith(b"\n") or value.endswith(b"\n\n"):
            issues.append(
                StructuralIssue(
                    code="NON_CANONICAL_LINE_ENDINGS",
                    category="incomplete_page",
                    path=key,
                    target=None,
                    repairable=True,
                    detail=key,
                )
            )

        if is_expected and value != expected[key]:
            if _is_index_artifact(key):
                issues.append(
                    StructuralIssue(
                        code="INDEX_CONTENT_MISMATCH",
                        category="index_inconsistency",
                        path=key,
                        target=None,
                        repairable=True,
                        detail=key,
                    )
                )
            else:
                issues.append(
                    StructuralIssue(
                        code="PAGE_CONTENT_MISMATCH",
                        category="incomplete_page",
                        path=key,
                        target=None,
                        repairable=True,
                        detail=key,
                    )
                )

        # Every strict-UTF-8 artifact is scanned for WikiLinks, including
        # unsafe and unexpected files, using the path with the ``wiki/``
        # prefix removed as the source path.  Content mismatch or line
        # endings must never suppress link validation.
        source_path = key[len("wiki/"):]
        issues.extend(_scan_links(state, expected_keys, source_path, text, index=index))

    # Structured links are a publication contract, unlike legacy
    # ``related_pages`` which historically tolerated model-generated noise.
    # A typed navigation/supported-relation edge must resolve to a page in the
    # same immutable Wiki snapshot; otherwise publication would expose a
    # machine-readable route that the reader cannot follow.
    issues.extend(_validate_structured_links(state))

    # Inspect related_pages so dangling/self relations are logged even when
    # the source page artifact is missing.  A real LLM's related_pages are
    # frequently dangling; degrade them to warnings, not fatal issues.
    for page_path, snapshot in state.pages.items():
        for target in snapshot.related_pages:
            artifact = "wiki/" + target
            if artifact not in expected_keys:
                logger.warning("wiki related dangling | page=%s target=%r", page_path, target)
            elif target == page_path:
                logger.warning("wiki related self | page=%s target=%r", page_path, target)

    return StructuralReport(issues=issues)


def validate_wiki_delta(
    state: CompilerState,
    delta: WikiRenderDelta,
    impact: Optional[MutationImpact] = None,
    index: Optional[WikiRenderIndex | WikiRenderIndexOverlay] = None,
) -> StructuralReport:
    """Validate only the artifacts in one incremental render.

    The canonical comparison is local: it re-renders the exact impact set,
    never the complete Wiki.  Full-tree byte equality remains a test oracle,
    not a production acceptance operation.
    """
    if not isinstance(delta, WikiRenderDelta):
        return StructuralReport(
            issues=[
                StructuralIssue(
                    code="INVALID_WIKI_DELTA",
                    category="index_inconsistency",
                    path="wiki/index.md",
                    repairable=False,
                    detail="delta must be WikiRenderDelta",
                )
            ]
        )

    effective_impact = impact or delta.impact
    if effective_impact is None:
        return StructuralReport(
            issues=[
                StructuralIssue(
                    code="MISSING_MUTATION_IMPACT",
                    category="index_inconsistency",
                    path="wiki/index.md",
                    repairable=False,
                    detail="delta has no MutationImpact",
                )
            ]
        )

    render_index = index or WikiRenderIndex.from_state(state)
    expected_delta = render_wiki_delta(state, effective_impact, render_index)
    issues: List[StructuralIssue] = _validate_structured_links(state)
    expected_files = expected_delta.files

    if set(delta.files) != set(expected_files):
        for key in sorted(set(expected_files) - set(delta.files)):
            issues.append(
                StructuralIssue(
                    code="MISSING_DELTA_ARTIFACT",
                    category=(
                        "incomplete_page"
                        if not _is_index_artifact(key)
                        else "index_inconsistency"
                    ),
                    path=key,
                    repairable=True,
                    detail=key,
                )
            )
        for key in sorted(set(delta.files) - set(expected_files)):
            issues.append(
                StructuralIssue(
                    code="UNEXPECTED_DELTA_ARTIFACT",
                    category="index_inconsistency",
                    path=key,
                    repairable=True,
                    detail=key,
                )
            )

    if tuple(delta.removed_artifacts) != tuple(expected_delta.removed_artifacts):
        for key in sorted(
            set(expected_delta.removed_artifacts) - set(delta.removed_artifacts)
        ):
            issues.append(
                StructuralIssue(
                    code="MISSING_DELTA_REMOVAL",
                    category="index_inconsistency",
                    path=key,
                    repairable=True,
                    detail=key,
                )
            )
        for key in sorted(
            set(delta.removed_artifacts) - set(expected_delta.removed_artifacts)
        ):
            issues.append(
                StructuralIssue(
                    code="UNEXPECTED_DELTA_REMOVAL",
                    category="index_inconsistency",
                    path=key,
                    repairable=True,
                    detail=key,
                )
            )

    if set(delta.files) & set(delta.removed_artifacts):
        for key in sorted(set(delta.files) & set(delta.removed_artifacts)):
            issues.append(
                StructuralIssue(
                    code="DELTA_WRITE_REMOVE_CONFLICT",
                    category="index_inconsistency",
                    path=key,
                    repairable=False,
                    detail=key,
                )
            )

    for key, value in delta.files.items():
        safe = _is_safe_artifact_key(key)
        if not safe:
            issues.append(
                StructuralIssue(
                    code="UNSAFE_ARTIFACT_PATH",
                    category="malformed_reference",
                    path=str(key),
                    repairable=True,
                    detail=str(key),
                )
            )
        if not isinstance(value, bytes):
            issues.append(
                StructuralIssue(
                    code="NON_BYTES_ARTIFACT",
                    category="incomplete_page",
                    path=str(key),
                    repairable=True,
                    detail=str(key),
                )
            )
            continue
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            issues.append(
                StructuralIssue(
                    code="INVALID_UTF8",
                    category="incomplete_page",
                    path=key,
                    repairable=True,
                    detail=key,
                )
            )
            continue
        if "\r" in text or not value.endswith(b"\n") or value.endswith(b"\n\n"):
            issues.append(
                StructuralIssue(
                    code="NON_CANONICAL_LINE_ENDINGS",
                    category="incomplete_page",
                    path=key,
                    repairable=True,
                    detail=key,
                )
            )
        if key in expected_files and value != expected_files[key]:
            issues.append(
                StructuralIssue(
                    code=(
                        "INDEX_CONTENT_MISMATCH"
                        if _is_index_artifact(key)
                        else "PAGE_CONTENT_MISMATCH"
                    ),
                    category=(
                        "index_inconsistency"
                        if _is_index_artifact(key)
                        else "incomplete_page"
                    ),
                    path=key,
                    repairable=True,
                    detail=key,
                )
            )
        issues.extend(
            _scan_links(
                state,
                None,
                key[len("wiki/") :] if key.startswith("wiki/") else key,
                text,
                index=render_index,
            )
        )

    for key in delta.removed_artifacts:
        if not _is_safe_artifact_key(key):
            issues.append(
                StructuralIssue(
                    code="UNSAFE_ARTIFACT_PATH",
                    category="malformed_reference",
                    path=str(key),
                    repairable=True,
                    detail=str(key),
                )
            )
            continue
        relative = key[len("wiki/") :]
        if relative == "index.md":
            issues.append(
                StructuralIssue(
                    code="INVALID_GLOBAL_INDEX_REMOVAL",
                    category="index_inconsistency",
                    path=key,
                    repairable=False,
                    detail=key,
                )
            )
        elif relative.endswith("/index.md"):
            directory = relative[: -len("/index.md")]
            if render_index.has_directory(directory):
                issues.append(
                    StructuralIssue(
                        code="REMOVED_LIVE_DIRECTORY_INDEX",
                        category="index_inconsistency",
                        path=key,
                        repairable=False,
                        detail=key,
                    )
                )
        elif relative in state.pages:
            issues.append(
                StructuralIssue(
                    code="REMOVED_LIVE_PAGE",
                    category="incomplete_page",
                    path=key,
                    repairable=False,
                    detail=key,
                )
            )

    return StructuralReport(issues=issues)


# ──────────────────────────────────────────────
#  Deterministic repair
# ──────────────────────────────────────────────


def repair_wiki_tree(
    state: CompilerState,
    files: Dict[str, bytes],
    *,
    index: Optional[WikiRenderIndex | WikiRenderIndexOverlay] = None,
) -> Dict[str, bytes]:
    """Rebuild the renderer-owned artifact mapping from an accepted state.

    Returns a new ``render_wiki_tree(state)`` mapping, replacing corrupt
    renderer-owned bytes, recreating missing files and removing unexpected
    files without mutating either input.  Because ``state`` is not changed,
    dangling/self/malformed references originating in state content remain
    after repair and are only reported by a subsequent ``validate_wiki_tree``.
    A repair attempt is never proof of closure.
    """
    del files
    return render_wiki_tree(state, index=index)
