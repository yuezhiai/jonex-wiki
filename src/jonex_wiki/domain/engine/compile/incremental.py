"""Runtime dependency indexes for incremental Wiki rendering.

The compiler state remains the durable source of truth.  ``WikiRenderIndex``
is a rebuildable runtime sidecar containing only relationships needed by the
deterministic renderer.  Workers share one read-only index and record small
overlays; the master index is changed only at the deterministic merge point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence, Set, Tuple, Union

from jonex_wiki.domain.engine.models.compiler_state import CompilerState


PageEdges = Optional[Tuple[str, ...]]
IndexLike = Union["WikiRenderIndex", "WikiRenderIndexOverlay"]


def _ancestors(page_path: str) -> Tuple[str, ...]:
    parts = page_path.split("/")
    return tuple("/".join(parts[:i]) for i in range(1, len(parts)))


def _parent(directory: str) -> Optional[str]:
    return directory.rsplit("/", 1)[0] if "/" in directory else None


def _direct_parent(page_path: str) -> str:
    return page_path.rsplit("/", 1)[0]


@dataclass(frozen=True)
class MutationImpact:
    """Exact renderer-owned artifacts affected by one mutation set."""

    render_pages: Tuple[str, ...] = ()
    render_directories: Tuple[str, ...] = ()
    render_global_index: bool = False
    remove_artifacts: Tuple[str, ...] = ()
    added_edges: Tuple[Tuple[str, str], ...] = ()
    removed_edges: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RenderIndexDelta:
    """Small, deterministic update applied to a master render index.

    Each item records the final outgoing edge set for one source page.  A
    ``None`` value means that the source page was deleted.  Setting final
    values (rather than replaying a before/after diff) makes deterministic
    last-write-wins parallel merges safe.
    """

    page_edges: Tuple[Tuple[str, PageEdges], ...] = ()

    @classmethod
    def from_mapping(cls, values: Dict[str, PageEdges]) -> "RenderIndexDelta":
        return cls(
            page_edges=tuple(
                (path, values[path]) for path in sorted(values)
            )
        )


@dataclass(frozen=True)
class WikiRenderDelta:
    """Rendered writes and removals for one ``MutationImpact``."""

    files: Dict[str, bytes]
    removed_artifacts: Tuple[str, ...] = ()
    impact: Optional[MutationImpact] = None


@dataclass
class WikiRenderIndex:
    """Mutable master index, normally updated only at a merge boundary."""

    source_edges: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    inbound_by_target: Dict[str, Set[str]] = field(default_factory=dict)
    direct_pages_by_dir: Dict[str, Set[str]] = field(default_factory=dict)
    child_dirs_by_dir: Dict[str, Set[str]] = field(default_factory=dict)
    directory_page_counts: Dict[str, int] = field(default_factory=dict)
    top_level_dirs: Set[str] = field(default_factory=set)

    @classmethod
    def from_state(cls, state: CompilerState) -> "WikiRenderIndex":
        index = cls()
        for path in sorted(state.pages):
            index._set_page(path, tuple(state.pages[path].related_pages))
        return index

    def overlay(self) -> "WikiRenderIndexOverlay":
        return WikiRenderIndexOverlay(self)

    def snapshot(self) -> "WikiRenderIndex":
        """Return an O(1) read-only view of the current index generation.

        ``apply`` uses copy-on-write for its top-level maps and touched sets,
        so these references stay stable while the master advances.  A worker
        can therefore hold the snapshot across an async model call without
        copying the complete page graph.
        """
        return WikiRenderIndex(
            source_edges=self.source_edges,
            inbound_by_target=self.inbound_by_target,
            direct_pages_by_dir=self.direct_pages_by_dir,
            child_dirs_by_dir=self.child_dirs_by_dir,
            directory_page_counts=self.directory_page_counts,
            top_level_dirs=self.top_level_dirs,
        )

    def page_edges(self, path: str) -> PageEdges:
        return self.source_edges.get(path)

    def inbound(self, target: str) -> Set[str]:
        return set(self.inbound_by_target.get(target, set()))

    def direct_pages(self, directory: str) -> Set[str]:
        return set(self.direct_pages_by_dir.get(directory, set()))

    def child_dirs(self, directory: str) -> Set[str]:
        return set(self.child_dirs_by_dir.get(directory, set()))

    def directories(self) -> Set[str]:
        return {
            directory
            for directory, count in self.directory_page_counts.items()
            if count > 0
        }

    def has_directory(self, directory: str) -> bool:
        return self.directory_page_counts.get(directory, 0) > 0

    def apply(self, delta: RenderIndexDelta) -> None:
        if not delta.page_edges:
            return
        # Detach the maps once per merge.  Individual sets are copied lazily
        # only when a touched key is changed; untouched generations remain
        # shared with worker snapshots.
        self.source_edges = dict(self.source_edges)
        self.inbound_by_target = dict(self.inbound_by_target)
        self.direct_pages_by_dir = dict(self.direct_pages_by_dir)
        self.child_dirs_by_dir = dict(self.child_dirs_by_dir)
        self.directory_page_counts = dict(self.directory_page_counts)
        self.top_level_dirs = set(self.top_level_dirs)
        for path, edges in delta.page_edges:
            self._set_page(path, edges)

    def _set_page(self, path: str, edges: PageEdges) -> None:
        old_edges = self.source_edges.get(path)
        old_exists = old_edges is not None

        if old_exists:
            for target in set(old_edges or ()):
                sources = self.inbound_by_target.get(target)
                if sources is not None:
                    sources = set(sources)
                    sources.discard(path)
                    if not sources:
                        self.inbound_by_target.pop(target, None)
                    else:
                        self.inbound_by_target[target] = sources

        if edges is None:
            if old_exists:
                self.source_edges.pop(path, None)
                self._update_membership(path, exists=False)
            return

        normalized = tuple(dict.fromkeys(edges))
        if not old_exists:
            self._update_membership(path, exists=True)
        self.source_edges[path] = normalized
        for target in set(normalized):
            sources = set(self.inbound_by_target.get(target, set()))
            sources.add(path)
            self.inbound_by_target[target] = sources

    def _update_membership(self, path: str, *, exists: bool) -> None:
        directory = _direct_parent(path)
        direct = set(self.direct_pages_by_dir.get(directory, set()))
        if exists:
            direct.add(path)
            self.direct_pages_by_dir[directory] = direct
            for ancestor in _ancestors(path):
                self._adjust_directory_count(ancestor, 1)
        else:
            direct.discard(path)
            if not direct:
                self.direct_pages_by_dir.pop(directory, None)
            else:
                self.direct_pages_by_dir[directory] = direct
            for ancestor in reversed(_ancestors(path)):
                self._adjust_directory_count(ancestor, -1)

    def _adjust_directory_count(self, directory: str, delta: int) -> None:
        old_count = self.directory_page_counts.get(directory, 0)
        new_count = old_count + delta
        if new_count <= 0:
            self.directory_page_counts.pop(directory, None)
            parent = _parent(directory)
            if parent is None:
                self.top_level_dirs.discard(directory)
            else:
                children = self.child_dirs_by_dir.get(parent)
                if children is not None:
                    children = set(children)
                    children.discard(directory)
                    if not children:
                        self.child_dirs_by_dir.pop(parent, None)
                    else:
                        self.child_dirs_by_dir[parent] = children
            return

        self.directory_page_counts[directory] = new_count
        if old_count == 0:
            parent = _parent(directory)
            if parent is None:
                self.top_level_dirs.add(directory)
            else:
                children = set(self.child_dirs_by_dir.get(parent, set()))
                children.add(directory)
                self.child_dirs_by_dir[parent] = children


@dataclass
class WikiRenderIndexOverlay:
    """Copy-on-write view used by one candidate or worker.

    Only paths touched by mutations are stored in ``overrides``.  All other
    lookups go to the shared base index, so a worker never copies O(P) maps.
    """

    base: IndexLike
    overrides: Dict[str, PageEdges] = field(default_factory=dict)

    def fork(self) -> "WikiRenderIndexOverlay":
        return WikiRenderIndexOverlay(self)

    def page_edges(self, path: str) -> PageEdges:
        if path in self.overrides:
            return self.overrides[path]
        return self.base.page_edges(path)

    def set_page(self, path: str, edges: PageEdges) -> None:
        self.overrides[path] = None if edges is None else tuple(dict.fromkeys(edges))

    def inbound(self, target: str) -> Set[str]:
        result = set(self.base.inbound(target))
        for path, new_edges in self.overrides.items():
            old_edges = self.base.page_edges(path)
            if old_edges is not None and target in old_edges:
                result.discard(path)
            if new_edges is not None and target in new_edges:
                result.add(path)
        return result

    def direct_pages(self, directory: str) -> Set[str]:
        result = set(self.base.direct_pages(directory))
        for path, new_edges in self.overrides.items():
            if _direct_parent(path) != directory:
                continue
            old_exists = self.base.page_edges(path) is not None
            new_exists = new_edges is not None
            if old_exists and not new_exists:
                result.discard(path)
            elif new_exists:
                result.add(path)
        return result

    def descendant_page_count(self, directory: str) -> int:
        count = (
            self.base.directory_page_counts.get(directory, 0)
            if isinstance(self.base, WikiRenderIndex)
            else self.base.descendant_page_count(directory)
        )
        for path, new_edges in self.overrides.items():
            old_exists = self.base.page_edges(path) is not None
            new_exists = new_edges is not None
            if old_exists == new_exists:
                continue
            if directory in _ancestors(path):
                count += 1 if new_exists else -1
        return max(0, count)

    def directories(self) -> Set[str]:
        candidates = set(self.base.directories())
        for path, new_edges in self.overrides.items():
            if new_edges is not None:
                candidates.update(_ancestors(path))
        return {
            directory
            for directory in candidates
            if self.descendant_page_count(directory) > 0
        }

    def has_directory(self, directory: str) -> bool:
        return self.descendant_page_count(directory) > 0

    def child_dirs(self, directory: str) -> Set[str]:
        before = self.base.directories()
        after = self.directories()
        result = set(self.base.child_dirs(directory))
        for changed in after - before:
            if _parent(changed) == directory:
                result.add(changed)
        for changed in before - after:
            if _parent(changed) == directory:
                result.discard(changed)
        return result

    def top_level_dirs(self) -> Set[str]:
        return {
            directory
            for directory in self.directories()
            if "/" not in directory
        }

    def to_delta(self) -> RenderIndexDelta:
        effective: Dict[str, PageEdges] = {}
        chain: list[WikiRenderIndexOverlay] = []
        current: IndexLike = self
        while isinstance(current, WikiRenderIndexOverlay):
            chain.append(current)
            current = current.base
        for overlay in reversed(chain):
            effective.update(overlay.overrides)
        return RenderIndexDelta.from_mapping(effective)


def _view(index: IndexLike) -> IndexLike:
    return index


def plan_mutation_impact(
    before_state: CompilerState,
    after_state: CompilerState,
    mutations: Sequence[object],
    index: Optional[IndexLike] = None,
) -> Tuple[MutationImpact, WikiRenderIndexOverlay]:
    """Plan the exact page/index closure for accepted mutation candidates."""

    base: IndexLike = index or WikiRenderIndex.from_state(before_state)
    overlay = base.fork() if isinstance(base, WikiRenderIndexOverlay) else base.overlay()
    render_pages: Set[str] = set()
    render_directories: Set[str] = set()
    remove_artifacts: Set[str] = set()
    added_edges: Set[Tuple[str, str]] = set()
    removed_edges: Set[Tuple[str, str]] = set()

    mutation_paths = []
    for mutation in mutations:
        path = getattr(mutation, "path")
        mutation_paths.append(path)
        before_page = before_state.pages.get(path)
        after_page = after_state.pages.get(path)
        old_edges = tuple(before_page.related_pages) if before_page is not None else ()
        new_edges = tuple(after_page.related_pages) if after_page is not None else ()
        old_set = set(old_edges)
        new_set = set(new_edges)
        added_edges.update((path, target) for target in new_set - old_set)
        removed_edges.update((path, target) for target in old_set - new_set)
        overlay.set_page(path, new_edges if after_page is not None else None)

        if after_page is not None:
            render_pages.add(path)
        elif before_page is not None:
            remove_artifacts.add("wiki/" + path)

        if before_page is None or after_page is None:
            directory = _direct_parent(path)
            if directory in overlay.directories():
                render_directories.add(directory)
        if before_page is not None and after_page is not None:
            if (
                before_page.index.title != after_page.index.title
                or before_page.index.description != after_page.index.description
            ):
                directory = _direct_parent(path)
                if directory in overlay.directories():
                    render_directories.add(directory)

        for target in (new_set | old_set):
            if target in after_state.pages and target != path:
                if (path, target) in added_edges or (path, target) in removed_edges:
                    render_pages.add(target)

        title_changed = (
            before_page is None
            or after_page is None
            or before_page.index.title != after_page.index.title
        )
        if title_changed:
            for source in base.inbound(path) | overlay.inbound(path):
                if source in after_state.pages:
                    render_pages.add(source)
            for target in old_set | new_set:
                if target in after_state.pages:
                    render_pages.add(target)

    before_dirs = base.directories()
    after_dirs = overlay.directories()
    changed_dirs = before_dirs ^ after_dirs
    for directory in changed_dirs:
        if directory in after_dirs:
            render_directories.add(directory)
        remove_artifact = "wiki/" + directory + "/index.md"
        if directory not in after_dirs:
            remove_artifacts.add(remove_artifact)
        parent = _parent(directory)
        if parent is not None and parent in after_dirs:
            render_directories.add(parent)

    before_top = base.top_level_dirs if isinstance(base, WikiRenderIndex) else {
        d for d in before_dirs if "/" not in d
    }
    after_top = overlay.top_level_dirs()

    return (
        MutationImpact(
            render_pages=tuple(sorted(render_pages)),
            render_directories=tuple(sorted(render_directories)),
            render_global_index=before_top != after_top,
            remove_artifacts=tuple(sorted(remove_artifacts)),
            added_edges=tuple(sorted(added_edges)),
            removed_edges=tuple(sorted(removed_edges)),
        ),
        overlay,
    )


__all__ = [
    "MutationImpact",
    "RenderIndexDelta",
    "WikiRenderDelta",
    "WikiRenderIndex",
    "WikiRenderIndexOverlay",
    "plan_mutation_impact",
]
