"""Identity, candidates and the entity/topic inverted index.

Two decisions that used to be one are separated here:

* *which page a fact belongs on* - that is S5's contribution record and its
  displays;
* *where a page points* - that is the page graph (``mention_index``).

Identity itself is decided in a fixed order, and it is allowed to say "I do not
know": an explicit identifier beats everything, a name plus its context can be
enough, and a bare name match across different contexts is *undetermined* - two
cities with one name are not one entity.  Fuzzy names are only *proposed* as
candidates from the existing lexical mention output (stable order, existing
scores, no vectors, no rerank, no source-diversity reordering); nothing is
merged automatically.

Relations that look like names are not names: a predecessor, successor, parent,
subsidiary or brand stays a relation and never becomes an alias.

Topic pages are first-class: not every fact has to belong to an entity.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

IDENTITY_RULE_VERSION = "wiki-identity-v1"

SAME = "same"
DIFFERENT = "different"
UNDETERMINED = "undetermined"

# Relation kinds that must never be folded into an alias list.
RELATION_KINDS = ("predecessor", "successor", "parent", "subsidiary", "brand")
KIND_ENTITY = "entity"
KIND_TOPIC = "topic"


def normalize_name(value: str) -> str:
    import unicodedata
    folded = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(folded.split())


def normalize_context(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


class NameForm(BaseModel):
    """One name as the material states it, plus what may qualify it."""

    name: str
    kind: str = KIND_ENTITY
    context: str = ""
    explicit_id: str = ""
    aliases: List[str] = Field(default_factory=list)
    relations: Dict[str, List[str]] = Field(default_factory=dict)
    page_path: str = ""

    model_config = ConfigDict(extra="forbid")

    @property
    def normalized(self) -> str:
        return normalize_name(self.name)

    def relation_targets(self, kind: str) -> List[str]:
        return [normalize_name(item) for item in self.relations.get(kind, [])]

    def alias_forms(self) -> List[str]:
        """Aliases only - an alias list that smuggles in a relation is ignored."""
        relation_names = {name for kind in RELATION_KINDS
                          for name in self.relation_targets(kind)}
        return [normalize_name(alias) for alias in self.aliases
                if normalize_name(alias) not in relation_names]


class IdentityCandidate(BaseModel):
    """A proposed pairing.  A proposal is not a merge."""

    left: str
    right: str
    decision: str
    basis: str
    context_match: bool = False
    relation: str = ""
    score: float = 0.0

    model_config = ConfigDict(extra="forbid")


def _relation_between(left: NameForm, right: NameForm) -> str:
    for kind in RELATION_KINDS:
        if right.normalized in left.relation_targets(kind):
            return kind
        if left.normalized in right.relation_targets(kind):
            return kind
    return ""


def decide_identity(left: NameForm, right: NameForm) -> IdentityCandidate:
    """Decide one pairing: same, different, or honestly undetermined."""
    base = {"left": left.normalized, "right": right.normalized}
    if left.explicit_id and right.explicit_id:
        if left.explicit_id == right.explicit_id:
            return IdentityCandidate(**base, decision=SAME, basis="explicit_identifier")
        return IdentityCandidate(**base, decision=DIFFERENT, basis="conflicting_identifier")
    same_name = left.normalized == right.normalized
    alias_match = (right.normalized in left.alias_forms()
                   or left.normalized in right.alias_forms())
    same_context = bool(normalize_context(left.context)) and normalize_context(left.context) == normalize_context(right.context)
    relation = _relation_between(left, right)
    if relation:
        # A predecessor is not another name for the same thing.
        return IdentityCandidate(**base, decision=DIFFERENT, basis="relation_not_alias",
                                 context_match=same_context, relation=relation)
    if left.kind != right.kind:
        # An entity and a topic may share a name; that is not one thing.
        return IdentityCandidate(**base, decision=UNDETERMINED,
                                 basis="topic_kept_separate",
                                 context_match=same_context)
    if (same_name or alias_match) and same_context:
        return IdentityCandidate(**base, decision=SAME,
                                 basis=("name_and_context" if same_name
                                        else "alias_and_context"),
                                 context_match=True)
    if same_name or alias_match:
        return IdentityCandidate(**base, decision=UNDETERMINED,
                                 basis="same_name_other_context", context_match=False)
    return IdentityCandidate(**base, decision=UNDETERMINED, basis="no_identifier")


def decide_pairings(forms: Sequence[NameForm]) -> List[IdentityCandidate]:
    """Every pairing, each decided on its own; nothing is merged transitively."""
    out: List[IdentityCandidate] = []
    ordered = list(forms)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            out.append(decide_identity(left, right))
    return out


def lexical_candidates(name: str, mentions: Mapping[str, Any], *,
                       limit: int = 8) -> List[Dict[str, Any]]:
    """Candidates for a fuzzy name from the existing mention output.

    No new scoring is invented: mentions already carry the page and the score
    the compiler produced, and they are read in that order.
    """
    wanted = normalize_name(name)
    if not wanted:
        return []
    found: List[Dict[str, Any]] = []
    for path, entry in sorted(dict(mentions or {}).items()):
        matches = entry if isinstance(entry, list) else [entry]
        if not any(wanted in normalize_name(str(item)) for item in matches):
            continue
        found.append({"name": name, "page_path": str(path), "decision": UNDETERMINED,
                      "basis": "lexical_candidate"})
        if len(found) >= limit:
            break
    return found


class IdentityIndex(BaseModel):
    """Entity/topic -> facts and pages, and fact -> pages."""

    version: str = IDENTITY_RULE_VERSION
    entity_to_facts: Dict[str, List[str]] = Field(default_factory=dict)
    entity_to_pages: Dict[str, List[str]] = Field(default_factory=dict)
    topic_to_facts: Dict[str, List[str]] = Field(default_factory=dict)
    topic_to_pages: Dict[str, List[str]] = Field(default_factory=dict)
    fact_to_pages: Dict[str, List[str]] = Field(default_factory=dict)
    source_versions: Dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    def facts_for(self, name: str) -> List[str]:
        key = normalize_name(name)
        return sorted(set(self.entity_to_facts.get(key, ()))
                      | set(self.topic_to_facts.get(key, ())))

    def pages_for(self, name: str) -> List[str]:
        key = normalize_name(name)
        return sorted(set(self.entity_to_pages.get(key, ()))
                      | set(self.topic_to_pages.get(key, ())))


def _add(bucket: Dict[str, List[str]], key: str, value: str) -> None:
    if not key:
        return
    values = bucket.setdefault(key, [])
    if value not in values:
        values.append(value)


def build_identity_index(contributions: Any, *,
                         source_versions: Optional[Mapping[str, str]] = None,
                         topic_pages: Optional[Mapping[str, Iterable[str]]] = None,
                         ) -> IdentityIndex:
    """Index every entity/topic mention to its facts and pages."""
    index = IdentityIndex(source_versions={str(key): str(value) for key, value
                                           in sorted(dict(source_versions or {}).items())})
    for record in getattr(contributions, "records", ()) or ():
        pages = sorted({display.page_path for display in record.displays})
        for page in pages:
            _add(index.fact_to_pages, record.record_id, page)
        for name in record.entity_mentions or ():
            key = normalize_name(name)
            _add(index.entity_to_facts, key, record.record_id)
            for page in pages:
                _add(index.entity_to_pages, key, page)
        hint = normalize_name(record.topic_hint)
        if hint:
            _add(index.topic_to_facts, hint, record.record_id)
            for page in pages:
                _add(index.topic_to_pages, hint, page)
    for topic, pages in sorted(dict(topic_pages or {}).items()):
        key = normalize_name(topic)
        for page in pages:
            _add(index.topic_to_pages, key, str(page))
    for bucket in (index.entity_to_facts, index.entity_to_pages, index.topic_to_facts,
                   index.topic_to_pages, index.fact_to_pages):
        for key in list(bucket):
            bucket[key] = sorted(bucket[key])
    return index


def update_identity_index(index: IdentityIndex, *, contributions: Any,
                          source_versions: Mapping[str, str],
                          affected_sources: Iterable[str] = ()) -> IdentityIndex:
    """Rebuild only the entries an affected source can have touched."""
    # Every map is a projection of the live contribution set. Page values and
    # fact values are different domains; filtering both as fact IDs retained
    # deleted pages forever. Rebuild deterministically until a measured need
    # justifies storing the reverse dependencies required for partial updates.
    return build_identity_index(contributions, source_versions=source_versions)


__all__ = [
    "DIFFERENT",
    "IDENTITY_RULE_VERSION",
    "KIND_ENTITY",
    "KIND_TOPIC",
    "RELATION_KINDS",
    "SAME",
    "UNDETERMINED",
    "IdentityCandidate",
    "IdentityIndex",
    "NameForm",
    "build_identity_index",
    "decide_identity",
    "decide_pairings",
    "lexical_candidates",
    "normalize_context",
    "normalize_name",
    "update_identity_index",
]
