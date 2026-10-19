"""Reading-side bundle access shared by the standalone service and platform."""

from jonex_wiki.domain.reading.bundle_reader import (
    count_pages,
    get_backlinks,
    get_claims,
    get_references,
    list_page_documents,
    list_pages,
    merge_map,
    merge_redirects,
    read_page,
)
from jonex_wiki.domain.reading.page_merge import (
    MergeMap,
    build_merge_map,
    normalize_title,
    remap_outline_paths,
)

__all__ = [
    "MergeMap",
    "build_merge_map",
    "count_pages",
    "get_backlinks",
    "get_claims",
    "get_references",
    "list_page_documents",
    "list_pages",
    "merge_map",
    "merge_redirects",
    "normalize_title",
    "read_page",
    "remap_outline_paths",
]
