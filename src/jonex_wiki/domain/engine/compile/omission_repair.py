"""A restricted Error Book path for confirmed compile omissions.

``COMPILE_INFORMATION_OMITTED`` is not a second discovery engine and not another
member of the blocking repair loop.  It exists for exactly one situation: a
*confirmed* compile omission - the source holds the information, the generated
page does not express it, and someone verified that against the original content
(see ``coverage_diagnostic.confirm_omission``).  Everything else stays out:
entity confusion, a page that was never read, a question the source never
answered and a calculation error are different problems with different owners.

The path is deliberately narrow:

* one attempt per source version and omission item, tracked in the entry's own
  ``repair_history``;
* a short range is restored as its full excerpt, a long one only as the
  confirmed evidence range - never a whole-page rewrite and never a wider range;
* the record closes only after verification: source binding, the omitted
  content is now visible, and the page's previous content is still there;
* failure leaves the entry open with its source entry point intact, and a
  changed source is re-judged under its new version instead of being retried.

No model is called anywhere in this module: the repair is a deterministic
append of verified source text.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from jonex_wiki.domain.engine.compile.error_book import (
    ErrorBook,
    ErrorBookEntry,
    RepairAttemptRecord,
    _derive_error_id,
)

OMISSION_RULE_VERSION = "compile-information-omitted-v1"
OMISSION_CODE = "COMPILE_INFORMATION_OMITTED"
OMISSION_CATEGORY = "coverage_gap"
MAX_OMISSION_ATTEMPTS = 1
# Short material is restored whole; longer material is restored only as the
# confirmed evidence range.  This is a repair-shape boundary, not an evidence
# budget: nothing outside the confirmed range is ever copied.
SHORT_RANGE_CHARS = 1000
CONFIRMED_STATUS = "confirmed_compile_omission"


class OmissionRepairPlan(BaseModel):
    """The single append one confirmed omission is allowed to make."""

    path: str
    excerpt: str
    range: List[int]
    source_id: str
    source_sha256: str
    full_excerpt: bool
    reason: str

    model_config = ConfigDict(extra="forbid")


def omission_attempts(entry: ErrorBookEntry) -> int:
    return sum(1 for attempt in entry.repair_history)


def _omission_key(entry: ErrorBookEntry) -> Tuple[str, str, Tuple[int, int], str]:
    return (str(entry.affected_source_ids[0] if entry.affected_source_ids else ""),
            str(entry.source_versions.get(
                entry.affected_source_ids[0] if entry.affected_source_ids else "", "")),
            (int(entry.omission_range[0]), int(entry.omission_range[1])),
            str(entry.affected_paths[0] if entry.affected_paths else ""))


def is_omission_repair_candidate(entry: ErrorBookEntry, *,
                                 current_versions: Mapping[str, str]) -> Tuple[bool, str]:
    """Whether this entry may be repaired *now*, and why not when it may not."""
    if entry.code != OMISSION_CODE:
        return False, "not_an_omission_entry"
    if entry.status != "open":
        return False, "not_open"
    if omission_attempts(entry) >= MAX_OMISSION_ATTEMPTS:
        return False, "attempt_limit_reached"
    source_id = str(entry.affected_source_ids[0]) if entry.affected_source_ids else ""
    bound = str(entry.source_versions.get(source_id, ""))
    if not bound or not str(entry.omission_source_sha256):
        return False, "source_binding_missing"
    if str(current_versions.get(source_id, "")) != bound:
        return False, "source_version_changed"
    if not entry.affected_paths:
        return False, "target_page_missing"
    return True, "eligible"


def open_omission(book: ErrorBook, *, confirmation: Mapping[str, Any],
                  batch_number: int, target_page: str,
                  source_id: Optional[str] = None,
                  passage_id: str = "") -> Tuple[ErrorBook, ErrorBookEntry]:
    """Record one confirmed omission, refusing anything that is not confirmed."""
    status = str(dict(confirmation).get("status") or "")
    if status != CONFIRMED_STATUS:
        raise ValueError(
            "OMISSION_NOT_CONFIRMED: only {} may open {} (got {!r})".format(
                CONFIRMED_STATUS, OMISSION_CODE, status))
    pages = [str(page) for page in (confirmation.get("target_pages") or ())]
    page = str(target_page or (pages[0] if pages else ""))
    if not page:
        raise ValueError("OMISSION_TARGET_PAGE_MISSING")
    resolved_source = str(source_id or confirmation.get("source_id") or "")
    if not resolved_source:
        raise ValueError("OMISSION_SOURCE_MISSING")
    start, end = (int(confirmation["range"][0]), int(confirmation["range"][1]))
    if start >= end:
        raise ValueError("OMISSION_RANGE_INVALID")
    version = str(confirmation.get("source_sha256") or "")
    if not version:
        raise ValueError("OMISSION_SOURCE_VERSION_MISSING")
    missing = [str(term) for term in (confirmation.get("missing_terms") or ())]
    detail = "{} chars [{}, {}) missing from {} @{}".format(
        end - start, start, end, page, version[:12])
    if missing:
        detail += "; missing terms: " + ", ".join(missing)
    entry = ErrorBookEntry(
        id="error_" + hashlib.sha256(repr((resolved_source, passage_id, version, start, end, page)).encode()).hexdigest()[:24],
        category=OMISSION_CATEGORY,
        code=OMISSION_CODE,
        phenomenon="confirmed compile omission: " + detail,
        error_class="Advisory",
        root_cause="the source states it and the generated page does not",
        constraint=("repair only the confirmed range on the target page; never "
                    "rewrite the page, never widen the range"),
        verification=("source binding matches, the omitted content is visible on "
                      "the page, and the page's previous content is unchanged"),
        affected_paths=[page],
        first_seen_batch=int(batch_number),
        last_seen_batch=int(batch_number),
        affected_source_ids=[resolved_source],
        affected_passage_ids=[str(passage_id)] if passage_id else [],
        omission_range=[start, end],
        omission_source_sha256=version,
        omission_description=str(confirmation.get("reason") or detail),
        omission_page_sha256=str(confirmation.get("page_snapshot_sha256") or ""),
        omission_range_sha256=str(confirmation.get("range_sha256") or ""),
        rule_version=OMISSION_RULE_VERSION,
        source_versions={resolved_source: version},
    )
    existing = next((old for old in book.entries if old.id == entry.id), None)
    if existing is not None:
        return book, existing
    kept = list(book.entries)
    superseded = []
    for existing in kept:
        if (existing.code == OMISSION_CODE and existing.status == "open"
                and existing.affected_passage_ids == entry.affected_passage_ids
                and _omission_key(existing)[:1] == _omission_key(entry)[:1]
                and _omission_key(existing)[2] == _omission_key(entry)[2]
                and _omission_key(existing)[3] == _omission_key(entry)[3]
                and _omission_key(existing)[1] != _omission_key(entry)[1]):
            superseded.append(existing.model_copy(update={
                "status": "closed", "resolution": "superseded_by_new_source_version",
                "last_seen_batch": int(batch_number)}))
    others = [existing for existing in kept if existing.id not in {e.id for e in superseded}]
    return ErrorBook(entries=sorted(others + superseded + [entry],
                                    key=lambda item: item.id)), entry


def plan_omission_repair(entry: ErrorBookEntry, *, source_text: str,
                         page_text: str,
                         current_versions: Mapping[str, str]) -> OmissionRepairPlan:
    """The bounded append for one eligible entry.  Raises when it is not eligible."""
    eligible, reason = is_omission_repair_candidate(entry, current_versions=current_versions)
    if not eligible:
        raise ValueError("OMISSION_NOT_ELIGIBLE:" + reason)
    source_id = str(entry.affected_source_ids[0])
    bound = str(entry.source_versions[source_id])
    if hashlib.sha256(str(source_text).encode("utf-8")).hexdigest() != bound:
        raise ValueError("OMISSION_SOURCE_CONTENT_MISMATCH")
    start, end = int(entry.omission_range[0]), int(entry.omission_range[1])
    text = str(source_text)
    if not 0 <= start < end <= len(text):
        raise ValueError("OMISSION_RANGE_OUT_OF_BOUNDS")
    excerpt = text[start:end]
    from jonex_wiki.domain.engine.compile.content_validation import _MAX_EXCERPT
    if len(excerpt) > _MAX_EXCERPT:
        raise ValueError("OMISSION_RANGE_EXCEEDS_EVIDENCE_LIMIT")
    if not entry.omission_page_sha256 or hashlib.sha256(page_text.encode()).hexdigest() != entry.omission_page_sha256:
        raise ValueError("OMISSION_PAGE_SNAPSHOT_CHANGED")
    if not entry.omission_range_sha256 or hashlib.sha256(excerpt.encode()).hexdigest() != entry.omission_range_sha256:
        raise ValueError("OMISSION_RANGE_CONTENT_MISMATCH")
    if not excerpt.strip():
        raise ValueError("OMISSION_RANGE_EMPTY")
    return OmissionRepairPlan(
        path=str(entry.affected_paths[0]),
        excerpt=excerpt,
        range=[start, end],
        source_id=source_id,
        source_sha256=bound,
        full_excerpt=len(excerpt) <= SHORT_RANGE_CHARS,
        reason=str(entry.omission_description),
    )


def apply_omission_repair(plan: OmissionRepairPlan, *, page_text: str) -> str:
    """Append the restored excerpt; the page's existing content is untouched."""
    body = str(page_text)
    addition = "\n\n## Restored source excerpt\n\n" + plan.excerpt.strip() + "\n"
    return body + addition


def verify_omission_repair(plan: OmissionRepairPlan, *, page_text: str,
                           previous_page_text: str,
                           source_text: str) -> Tuple[bool, List[str]]:
    """The three gates that must pass before the record may close."""
    issues: List[str] = []
    source = str(source_text)
    start, end = int(plan.range[0]), int(plan.range[1])
    if (hashlib.sha256(source.encode("utf-8")).hexdigest() != plan.source_sha256
            or not 0 <= start < end <= len(source)
            or source[start:end] != plan.excerpt):
        issues.append("OMISSION_SOURCE_BINDING_INVALID")
    body = str(page_text)
    if plan.excerpt.strip() and plan.excerpt.strip() not in body:
        issues.append("OMISSION_CONTENT_NOT_VISIBLE")
    previous = str(previous_page_text)
    if previous and previous.rstrip("\n") not in body:
        issues.append("OMISSION_PREVIOUS_CONTENT_LOST")
    return (not issues), issues


def repair_omission(book: ErrorBook, entry_id: str, *, source_text: str,
                    page_text: str,
                    current_versions: Mapping[str, str],
                    batch_number: int) -> Tuple[ErrorBook, Dict[str, Any]]:
    """Run the one allowed attempt and close the record only if it verifies.

    Returns the updated book and a bounded outcome record.  A failure leaves the
    entry open with its source entry point (``affected_paths`` and the bound
    range) intact so the next judgement starts from the original material.
    """
    entry = next((item for item in book.entries if item.id == entry_id), None)
    if entry is None:
        raise KeyError("OMISSION_ENTRY_NOT_FOUND:" + str(entry_id))
    eligible, reason = is_omission_repair_candidate(entry, current_versions=current_versions)
    if not eligible:
        return book, {"entry_id": entry_id, "accepted": False, "reason": reason,
                      "mutated_paths": []}
    plan = None
    repaired = page_text
    try:
        plan = plan_omission_repair(entry, source_text=source_text, page_text=page_text,
                                    current_versions=current_versions)
        repaired = apply_omission_repair(plan, page_text=page_text)
        ok, issues = verify_omission_repair(plan, page_text=repaired,
                                            previous_page_text=page_text,
                                            source_text=source_text)
    except ValueError as exc:
        ok, issues = False, [str(exc)]
    attempt = RepairAttemptRecord(
        batch_number=int(batch_number),
        accepted=bool(ok),
        resolution=("omission_repaired" if ok else "omission_repair_failed"),
        mutated_paths=[plan.path] if ok else [],
        structural_issue_codes=[],
        content_issue_codes=[] if ok else list(issues),
    )
    updated = entry.model_copy(update={
        "status": "closed" if ok else "open",
        "resolution": "omission_repaired" if ok else None,
        "last_seen_batch": int(batch_number),
        "repair_history": list(entry.repair_history) + [attempt],
    })
    entries = [updated if item.id == entry_id else item for item in book.entries]
    outcome = {
        "entry_id": entry_id,
        "accepted": bool(ok),
        "reason": "omission_repaired" if ok else "verification_failed",
        "mutated_paths": [plan.path] if ok else [],
        "issues": list(issues),
        "page_text": repaired,
        "full_excerpt": plan.full_excerpt if plan else False,
        "range": list(entry.omission_range),
    }
    return ErrorBook(entries=sorted(entries, key=lambda item: item.id)), outcome


def apply_confirmed_omissions(state: Any, book: ErrorBook,
                              confirmations: Iterable[Mapping[str, Any]], *,
                              batch_number: int = 1) -> Tuple[Any, ErrorBook,
                                                               List[Dict[str, Any]]]:
    """The production seam: apply confirmed omissions to a compiling state.

    Only a confirmation that already carries ``status ==
    "confirmed_compile_omission"`` can reach the repair.  A question the source
    never answered, a page nobody read, an entity confusion or a calculation
    error produces a different status from the coverage diagnosis and is
    dropped here with its reason recorded - none of them becomes an Error Book
    row, let alone a repair.
    """
    outcomes: List[Dict[str, Any]] = []
    pages = dict(getattr(state, "pages", {}) or {})
    for confirmation in confirmations or ():
        item = dict(confirmation)
        if str(item.get("status") or "") != CONFIRMED_STATUS:
            outcomes.append({"accepted": False, "reason": "not_confirmed",
                             "status": str(item.get("status") or "")})
            continue
        passage_id = str(item.get("passage_id") or "")
        passage = dict(getattr(state, "source_archive", {}) or {}).get(passage_id)
        if passage is None:
            outcomes.append({"accepted": False, "reason": "source_not_archived",
                             "passage_id": passage_id})
            continue
        required = ("source_id", "source_sha256", "range_sha256", "page_snapshot_sha256", "reason")
        if any(not item.get(key) for key in required) or item["source_id"] != str(passage.source_id):
            outcomes.append({"accepted": False, "reason": "confirmation_binding_missing"})
            continue
        version = str(passage.sha256)
        if str(item.get("source_sha256") or version) != version:
            outcomes.append({"accepted": False, "reason": "source_version_changed",
                             "passage_id": passage_id})
            continue
        page_path = str(item.get("page_path") or
                        ((item.get("target_pages") or [""])[0]))
        snapshot = pages.get(page_path)
        if snapshot is None:
            outcomes.append({"accepted": False, "reason": "target_page_missing",
                             "page_path": page_path})
            continue
        book, entry = open_omission(book, confirmation=item,
                                    batch_number=batch_number,
                                    target_page=page_path,
                                    source_id=str(passage.source_id),
                                    passage_id=passage_id)
        book, outcome = repair_omission(
            book, entry.id,
            source_text=str(passage.text),
            page_text=str(snapshot.content),
            current_versions={str(passage.source_id): version},
            batch_number=batch_number)
        outcome = {**outcome, "passage_id": passage_id, "page_path": page_path}
        if outcome["accepted"]:
            pages[page_path] = snapshot.model_copy(update={
                "content": str(outcome.pop("page_text")),
                "source_ids": list(dict.fromkeys(list(snapshot.source_ids) + [passage_id])),
                "version": snapshot.version + 1})
        else:
            outcome.pop("page_text", None)
        outcomes.append(outcome)
    if any(item.get("accepted") for item in outcomes):
        state = state.model_copy(update={"pages": pages})
    return state, book, outcomes


__all__ = [
    "CONFIRMED_STATUS",
    "MAX_OMISSION_ATTEMPTS",
    "OMISSION_CATEGORY",
    "OMISSION_CODE",
    "OMISSION_RULE_VERSION",
    "SHORT_RANGE_CHARS",
    "OmissionRepairPlan",
    "apply_omission_repair",
    "is_omission_repair_candidate",
    "omission_attempts",
    "apply_confirmed_omissions",
    "open_omission",
    "plan_omission_repair",
    "repair_omission",
    "verify_omission_repair",
]
