"""Layered coverage diagnosis: where did a question's information go?

Four independent questions, answered from fields that already exist - never
from a second coverage ledger:

1. ``source_archived``  - is the information in the archived source at all?
2. ``page_expressed``   - does a generated page express it?
3. ``evidence_attached``- does the evidence attached to an admitted claim contain it?
4. ``query_delivered``  - did this query actually deliver it?

The distinction matters more than the boolean.  A missing answer is not
automatically a compilation omission: the source may never have contained the
information, the page may exist unread, or the text may have been delivered and
the answer still wrong.  ``semantic_coverage="not_proven"`` (see
``disposition_ledger``) says only that completeness was never proven; it is
never promoted to a confirmed omission here.

A *candidate* names what should be checked: the source version, the exact range,
the page that should carry it and the missing terms.  A *confirmation* requires
verifiable original content, a page snapshot and a reason - supplied by a
caller, never inferred from a question, an answer or a model's own label.

Pure and deterministic: no model, network, storage or filesystem access.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

COVERAGE_DIAGNOSTIC_VERSION = "compile-coverage-v1"

# A term is a word-like run (letters/digits, 3+ characters) or a CJK run (2+
# characters).  This is the same shallow lexical probe the offline review used
# for "is this text about the question at all"; it is a sampling hint, never a
# semantic proof of coverage.
_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-_]{2,}|[\u3400-\u9fff]{2,}")
_STOPWORDS = frozenset({
    "the", "and", "for", "was", "were", "are", "its", "his", "her", "their",
    "with", "from", "that", "this", "what", "which", "who", "whom", "whose",
    "how", "many", "much", "did", "does", "has", "have", "had", "into", "over",
    "per", "than", "then", "when", "where", "why", "not", "all", "any", "each",
    "about", "after", "before", "between", "during", "company", "companies",
})

STATUS_DELIVERED = "delivered"
STATUS_NOT_READ = "not_read_this_run"
STATUS_CANDIDATE = "compile_omission_candidate"
STATUS_SOURCE_LACKS = "source_lacks_information"
STATUS_NOT_PROVEN = "semantic_coverage_not_proven"


def question_terms(question: str, *, limit: int = 12) -> List[str]:
    """The distinct, ordered probe terms of one question."""
    seen: List[str] = []
    for match in _TERM_RE.finditer(str(question or "")):
        term = match.group(0).casefold()
        if term in _STOPWORDS or term in seen:
            continue
        seen.append(term)
        if len(seen) >= limit:
            break
    return seen


def _has_terms(text: str, terms: Sequence[str]) -> bool:
    if not terms:
        return False
    folded = str(text or "").casefold()
    return all(term in folded for term in terms)


_SOURCE_PAGE_PREFIXES = ("source-articles/", "source-evidence/", "sources/",
                         "raw-tables/", "tables/")


def _is_source_page(path: str) -> bool:
    return str(path).startswith(_SOURCE_PAGE_PREFIXES)


def _missing_terms(text: str, terms: Sequence[str]) -> List[str]:
    folded = str(text or "").casefold()
    return [term for term in terms if term not in folded]


def published_sources(corpus: Any) -> Dict[str, str]:
    """The published source text per source id, from the corpus itself.

    A consumer of a published Bundle does not hold the compile-time archive
    (``CompilerState.source_archive`` is not carried into the published state),
    so the first layer has to be answered from what the Bundle actually
    publishes: the exact archived intervals (``source_records``), plus the full
    article pages when a source has them.  Both are text a reader can reach.
    """
    sources: Dict[str, str] = {}
    for path, records in dict(getattr(corpus, "source_records", {}) or {}).items():
        for record in records or ():
            source_id = str(record.get("source_id") or record.get("document_id") or "")
            if not source_id:
                continue
            sources[source_id] = sources.get(source_id, "") + str(record.get("excerpt") or "")
    for path, text in dict(getattr(corpus, "page_text", {}) or {}).items():
        if not str(path).startswith("source-articles/"):
            continue
        entry = (getattr(corpus, "page_index", {}) or {}).get(path)
        scope = getattr(entry, "scope", None) or {}
        source_id = str(scope.get("source_id") or getattr(entry, "object_id", "") or "")
        if source_id and source_id not in sources:
            sources[source_id] = str(text or "")
    return sources


def _passage_rows(source_archive: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for passage_id, passage in sorted(dict(source_archive or {}).items()):
        text = str(getattr(passage, "text", "") or "")
        if not text:
            continue
        rows.append({
            "passage_id": str(passage_id),
            "source_id": str(getattr(passage, "source_id", "") or ""),
            "document_id": str(getattr(passage, "document_id", "") or ""),
            "chunk_id": str(getattr(passage, "chunk_id", "") or ""),
            "sha256": str(getattr(passage, "sha256", "") or ""),
            "text": text,
        })
    return rows


def claims_for_diagnosis(*, corpus: Any = None, claims: Iterable[Any] = ()) -> List[Any]:
    """Every claim the wiki can be judged by, from whichever side holds them.

    A published Bundle keeps its admitted claims on the query corpus
    (``corpus.claims``); a compile-time caller holds the validated list.  Both
    are the same objects, so the diagnosis takes their union instead of asking
    each caller to remember which side is populated.
    """
    merged: Dict[str, Any] = {}
    for claim in claims or ():
        merged[str(getattr(claim, "claim_id", "") or "")] = claim
    for value in dict(getattr(corpus, "claims", {}) or {}).items():
        entries = value[1] if isinstance(value, tuple) and len(value) == 2 else value
        for claim in (entries if isinstance(entries, (list, tuple)) else [entries]):
            if claim is None:
                continue
            merged[str(getattr(claim, "claim_id", "") or "")] = claim
    return [merged[key] for key in sorted(merged) if key]


def _claim_rows(claims: Iterable[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for claim in claims or ():
        evidence = []
        for ref in getattr(claim, "evidence", ()) or ():
            excerpt = getattr(ref, "excerpt", None)
            if excerpt is None and isinstance(ref, Mapping):
                excerpt = ref.get("excerpt")
            evidence.append(str(excerpt or ""))
        rows.append({
            "claim_id": str(getattr(claim, "claim_id", "") or ""),
            "page_path": str(getattr(claim, "page_path", "") or ""),
            "text": str(getattr(claim, "text", "") or ""),
            "support_status": str(getattr(claim, "support_status", "") or ""),
            "source_ids": sorted({str(ref.source_id) for ref in getattr(claim, "evidence", ())}),
            "evidence": evidence,
        })
    return rows


def _delivered_rows(result: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    # Final citations are a selected subset and may contain canonical text not
    # delivered in full. Only actual tool-delivery observations count here.
    for entry in getattr(result, "trace", ()) or ():
        if entry.event == "coverage_delivery_probe" and entry.detail.get("matched"):
            rows.append({"path": "", "text": " ".join(entry.detail.get("terms", ()))})
    return rows


def _page_targets(claim_rows: Sequence[Mapping[str, Any]],
                  passage_source: str) -> List[str]:
    """Pages that already carry a claim from this source, best-effort target."""
    return sorted({str(row["page_path"]) for row in claim_rows
                   if str(row.get("page_path") or "")
                   and passage_source in row.get("source_ids", ())})


def diagnose_coverage(
    *,
    question: str,
    source_archive: Optional[Mapping[str, Any]] = None,
    sources: Optional[Mapping[str, str]] = None,
    claims: Iterable[Any] = (),
    pages: Optional[Mapping[str, str]] = None,
    result: Any = None,
    semantic_coverage: str = "not_proven",
) -> Dict[str, Any]:
    """Answer the four coverage questions for one question/run pair.

    ``source_archive`` is the compile-time archive (``passage_id -> passage``);
    ``sources`` is the published text per source id (see
    :func:`published_sources`).  A caller that only holds a published Bundle
    passes ``sources``; a compile-time caller passes ``source_archive``.
    """
    terms = question_terms(question)
    passages = _passage_rows(source_archive)
    if not passages:
        passages = [
            {"passage_id": "source:" + source_id, "source_id": source_id,
             "document_id": source_id, "chunk_id": "", "sha256": hashlib.sha256(
                 str(text).encode("utf-8")).hexdigest(), "text": str(text)}
            for source_id, text in sorted(dict(sources or {}).items())
        ]
    claim_rows = _claim_rows(claims)
    page_texts = {str(path): str(text or "") for path, text in dict(pages or {}).items()}
    delivered = _delivered_rows(result)

    archive_hit = next((row for row in passages if _has_terms(row["text"], terms)), None)
    claim_hit = next((row for row in claim_rows if _has_terms(row["text"], terms)), None)
    evidence_hit = next((row for row in claim_rows
                         if any(_has_terms(excerpt, terms) for excerpt in row["evidence"])),
                        None)
    delivered_hit = next((row for row in delivered if _has_terms(row["text"], terms)), None)
    # Only a *generated* page can answer "did the wiki express this": the
    # source pages are layer 1 and would otherwise make every archived fact look
    # expressed.
    page_hit = next((path for path, text in sorted(page_texts.items())
                     if not _is_source_page(path) and _has_terms(text, terms)), None)

    layers = {
        "source_archived": bool(archive_hit),
        "page_expressed": bool(claim_hit) or bool(page_hit),
        "evidence_attached": bool(evidence_hit),
        "query_delivered": bool(delivered_hit),
    }

    if not layers["source_archived"]:
        status = STATUS_NOT_PROVEN
    elif layers["query_delivered"]:
        status = STATUS_DELIVERED
    elif layers["page_expressed"] or layers["evidence_attached"]:
        status = STATUS_NOT_READ
    else:
        status = STATUS_CANDIDATE

    # What the wiki does express for these terms: generated pages, claim texts
    # and the evidence attached to those claims.  A term the source contains but
    # this text does not is what a candidate is made of - the gap is between the
    # source and the wiki, never inside the source.
    expressed = " ".join(
        [text for path, text in sorted(page_texts.items()) if not _is_source_page(path)]
        + [row["text"] for row in claim_rows]
        + [excerpt for row in claim_rows for excerpt in row["evidence"]]
    )
    missing_from_wiki = _missing_terms(expressed, terms)

    candidates: List[Dict[str, Any]] = []
    if status == STATUS_CANDIDATE and missing_from_wiki:
        for row in passages:
            if not _has_terms(row["text"], terms):
                continue
            candidates.append({
                "source_id": row["source_id"],
                "document_id": row["document_id"],
                "chunk_id": row["chunk_id"],
                "passage_id": row["passage_id"],
                "source_sha256": row["sha256"],
                "range": [0, len(row["text"])],
                "range_sha256": hashlib.sha256(row["text"].encode("utf-8")).hexdigest(),
                "target_pages": _page_targets(claim_rows, row["source_id"]),
                "missing_terms": list(missing_from_wiki),
                "reason": "archived_interval_has_no_claim_or_evidence_for_the_question",
            })

    return {
        "version": COVERAGE_DIAGNOSTIC_VERSION,
        "question_terms": terms,
        "layers": layers,
        "observation_method": "lexical_probe_not_semantic_proof",
        "status": status,
        # Not proven is a statement about evidence, not about the compiler.
        "semantic_coverage": str(semantic_coverage or "not_proven"),
        "supported_claim_count": sum(1 for row in claim_rows
                                     if row["support_status"] == "SUPPORTED"),
        "candidates": candidates,
        "confirmed": [],
        "note": ("a candidate is a question to check, not a finding: confirmation "
                 "needs verifiable source content, a page snapshot and a reason"),
    }


def confirm_omission(candidate: Mapping[str, Any], *, source_text: str,
                     page_text: str, reason: str) -> Dict[str, Any]:
    """Turn one candidate into a confirmed omission, or refuse to.

    Confirmation is refused unless the candidate's range really is that slice of
    the source (content hash), the target page has a snapshot to compare
    against, and a reason is stated.  A question, an answer or a model's own
    label is not a reason.
    """
    start, end = (int(candidate["range"][0]), int(candidate["range"][1]))
    text = str(source_text or "")
    if not 0 <= start < end <= len(text):
        raise ValueError("COVERAGE_RANGE_OUT_OF_BOUNDS")
    slice_text = text[start:end]
    digest = hashlib.sha256(slice_text.encode("utf-8")).hexdigest()
    if digest != str(candidate.get("range_sha256") or ""):
        raise ValueError("COVERAGE_RANGE_CONTENT_MISMATCH")
    snapshot = str(page_text or "")
    if not snapshot.strip():
        raise ValueError("COVERAGE_PAGE_SNAPSHOT_MISSING")
    label = " ".join(str(reason or "").split())
    if not label:
        raise ValueError("COVERAGE_REASON_MISSING")
    return {
        "status": "confirmed_compile_omission",
        "passage_id": str(candidate.get("passage_id") or ""),
        "version": COVERAGE_DIAGNOSTIC_VERSION,
        "source_id": str(candidate.get("source_id") or ""),
        "source_sha256": str(candidate.get("source_sha256") or ""),
        "range": [start, end],
        "range_sha256": digest,
        "target_pages": list(candidate.get("target_pages") or ()),
        "page_snapshot_sha256": hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
        "missing_terms": list(candidate.get("missing_terms") or ()),
        "reason": label,
    }


__all__ = [
    "COVERAGE_DIAGNOSTIC_VERSION",
    "STATUS_CANDIDATE",
    "STATUS_DELIVERED",
    "STATUS_NOT_PROVEN",
    "STATUS_NOT_READ",
    "STATUS_SOURCE_LACKS",
    "claims_for_diagnosis",
    "confirm_omission",
    "diagnose_coverage",
    "published_sources",
    "question_terms",
]
