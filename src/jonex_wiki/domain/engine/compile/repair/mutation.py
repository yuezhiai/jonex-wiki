"""Mutation path repair (lwp-arch-001 ALG1).

Extracted verbatim from ``engines/llm_wiki_plus/algorithm1.py``: the bounded
edit-distance guard (``_path_edit_distance_at_most``) and the duplicated-path
drift repair (``_repair_combined_response_paths``) applied at the
response boundary before Pydantic validation.  Pure: no provider, network,
filesystem, storage or database access.  The old import path stays available
through the re-export shim.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from jonex_wiki.domain.engine.models.compiler_state import normalize_wiki_path

logger = logging.getLogger(__name__)



def _path_edit_distance_at_most(left: str, right: str, limit: int) -> bool:
    """Return whether two path strings differ by at most ``limit`` edits.

    This bounded check is used only after requiring the same parent directory
    and a selected snapshot path, so it is a typo guard rather than a path
    resolver.
    """
    if abs(len(left) - len(right)) > limit:
        return False
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        row_min = current[0]
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            value = min(
                current[j - 1] + 1,
                previous[j] + 1,
                previous[j - 1] + cost,
            )
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return False
        previous = current
    return previous[-1] <= limit


def _repair_combined_response_paths(raw: Any) -> Any:
    """Repair one safe class of duplicated-path drift in model output.

    ``PageMutation.path`` and ``PageMutation.page.index.path`` repeat the same
    target by design.  A typo in one copy otherwise rejects the whole passage
    before deterministic evidence checks can run.  The page snapshot is used
    as the source of truth only when its path is selected, the two paths share
    a parent directory, the names are within four edits, and the mutation path
    is not itself another selected target.  All other mismatches remain
    fail-closed at the Pydantic boundary.
    """
    if not isinstance(raw, dict):
        return raw
    selection = raw.get("selection")
    proposal = raw.get("proposal")
    if not isinstance(selection, dict) or not isinstance(proposal, dict):
        return raw

    selected_paths = set()
    for value in selection.get("existing_paths", []) or []:
        try:
            selected_paths.add(normalize_wiki_path(value))
        except (TypeError, ValueError):
            continue
    for item in selection.get("new_pages", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            selected_paths.add(normalize_wiki_path(item.get("path")))
        except (TypeError, ValueError):
            continue
    if not selected_paths:
        return raw

    mutations = proposal.get("mutations")
    if not isinstance(mutations, list):
        return raw
    repaired = None
    for index, mutation in enumerate(mutations):
        if not isinstance(mutation, dict):
            continue
        if mutation.get("operation") not in ("create", "update"):
            continue
        page = mutation.get("page")
        page_index = page.get("index") if isinstance(page, dict) else None
        if not isinstance(page_index, dict):
            continue
        try:
            mutation_path = normalize_wiki_path(mutation.get("path"))
            snapshot_path = normalize_wiki_path(page_index.get("path"))
        except (TypeError, ValueError):
            continue
        if mutation_path == snapshot_path:
            continue
        if snapshot_path not in selected_paths or mutation_path in selected_paths:
            continue
        mutation_parent, _, mutation_name = mutation_path.rpartition("/")
        snapshot_parent, _, snapshot_name = snapshot_path.rpartition("/")
        if mutation_parent != snapshot_parent:
            continue
        if len(mutation_name) < 12 or len(snapshot_name) < 12:
            continue
        if not _path_edit_distance_at_most(mutation_name, snapshot_name, 4):
            continue
        if repaired is None:
            repaired = deepcopy(raw)
        repaired["proposal"]["mutations"][index]["path"] = snapshot_path
        logger.warning(
            "repaired duplicated mutation path drift | mutation=%s snapshot=%s",
            mutation_path,
            snapshot_path,
        )
    return repaired if repaired is not None else raw
