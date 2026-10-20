"""— pure engine-side query-intent routing.

Route is a per-query *runtime* axis (run.config / trace), never part of the
effective-config contract and never persisted.  ``resolve_route`` classifies a
question into one of four deterministic routes:

- ``proposition``    claim-anchored hybrid retrieval (the current default stack);
- ``lexical``        FTS-only lookup for numbered / keyword / code identifiers;
- ``chapter_tree``   chapter/navigation questions over the wiki render tree;
- ``summary``        deterministic bounded summary of the active evidence.

Pure by construction: the module has no I/O, no clock, no database access, and
no randomness.  Route override flows through the ``explicit`` argument only;
the production default path stays a pure function of the question.

The trigger rule table below is the single source of truth for the four-route
classification; it is evaluated in order and the first match wins, so the same
question always resolves to the same route.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional, Tuple


class Route(str, Enum):
    """The four long-document routing intents."""

    PROPOSITION = "proposition"
    LEXICAL = "lexical"
    CHAPTER_TREE = "chapter_tree"
    SUMMARY = "summary"


# ──────────────────────────────────────────────
#  Deterministic trigger rule table
# ──────────────────────────────────────────────
# Each rule is (Route, compiled pattern).  Rules are evaluated in order; the
# first pattern match wins.  ``summary`` outranks structural/numbered markers
# so "总结第3章的内容" is a summary request, not a numbered lookup; structural
# navigation outranks plain numbered lookups so "第3章在哪/有哪些章节" lands on
# the chapter-tree route; numbered/code/identifier lookups land on ``lexical``;
# everything else stays on the default ``proposition`` hybrid stack.
_ROUTE_RULES: Tuple[Tuple[Route, "re.Pattern[str]"], ...] = (
    (
        Route.SUMMARY,
        re.compile(
            r"(总结|摘要|概述|归纳|概括|简述|summarize|summary|overview|recap)",
            re.IGNORECASE,
        ),
    ),
    (
        Route.CHAPTER_TREE,
        re.compile(
            r"(目录|章节|导航|章节结构|章节树|章节列表|哪一章|有哪些章节|"
            r"结构一览|章节一览|章节分布|章节目录|contents|table\s+of\s+contents)",
            re.IGNORECASE,
        ),
    ),
    (
        Route.LEXICAL,
        re.compile(
            r"(第\s*[0-9０-９一二三四五六七八九十百千]+\s*[章节条款篇部分卷]|"
            r"section\s*\d+|chapter\s*\d+|article\s*\d+|article\s+[a-z]+|"
            r"条款\s*\d+|编号|序号|编号为|page\s*\d+|第\s*\d+\s*页|"
            r"[\u4e00-\u9fff]{1,4}[-－][A-Za-z0-9]+|"
            r"[A-Za-z]{1,4}[-－][A-Za-z0-9]+)",
            re.IGNORECASE,
        ),
    ),
)


def _normalize_route(value: object) -> Route:
    """Coerce a str/Route to ``Route``, failing closed on unknown values."""
    if isinstance(value, Route):
        return value
    if isinstance(value, str):
        try:
            return Route(value.strip().lower())
        except ValueError:
            raise ValueError(
                "UNKNOWN_ROUTE: {!r}; known routes: {}".format(
                    value, [r.value for r in Route]
                )
            ) from None
    raise ValueError(
        "UNKNOWN_ROUTE: {!r}; known routes: {}".format(value, [r.value for r in Route])
    )


def resolve_route(question: str, explicit: Optional[object] = None) -> Route:
    """Classify ``question`` into a ``Route``.

    Precedence:
      1. ``explicit`` — the per-query override passed through the routing
         layer (e.g. ``route="lexical"`` or the routing-dict pop);
      2. the deterministic trigger rule table;
      3. ``Route.PROPOSITION`` — the default hybrid stack, unchanged.

    Pure: no I/O, no clock, no DB.
    """
    if explicit is not None:
        # ``auto`` is the caller spelling for "no override": classify from the
        # question exactly as if no explicit route were supplied.
        if isinstance(explicit, str) and explicit.strip().casefold() == "auto":
            pass
        else:
            return _normalize_route(explicit)
    if not isinstance(question, str) or not question.strip():
        # A blank question is rejected later by the traversal (BLANK_QUESTION);
        # routing just falls back to the default stack.
        return Route.PROPOSITION
    for route, pattern in _ROUTE_RULES:
        if pattern.search(question):
            return route
    return Route.PROPOSITION


__all__ = [
    "Route",
    "resolve_route",
]
