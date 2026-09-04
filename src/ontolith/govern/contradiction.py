"""Contradiction model - unresolved conflicts between static assertions.

Per SPEC §10.3: when sources disagree on a static fact, a Contradiction object
groups the conflicting assertions for human review rather than silently overwriting.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Named alias (not inlined in Contradiction.state below) so every interface
# that validates a caller-supplied state filter (REST/GraphQL's list routes
# and MCP's ontolith.list_contradictions, KI-077) can derive its accepted-
# values set via ``typing.get_args(ContradictionState)`` from this one
# source of truth, instead of a hand-duplicated tuple per interface that
# could silently drift if this Literal ever gains or loses a state — same
# pattern ``plugins/registry.py`` already uses for ``PluginKind``.
ContradictionState = Literal["open", "resolved"]


class Contradiction(BaseModel):
    """Open or resolved conflict between static assertions.

    Attributes:
        id: Unique contradiction ID (ULID)
        namespace: Namespace this contradiction belongs to
        subject: Entity the conflicting assertions are about
        predicate: The predicate where sources disagree
        state: Whether the contradiction is open or resolved
        member_ids: IDs of the flagged assertions in this contradiction
        created_at: When this contradiction was first detected
        raised_by: Principal ID who raised it — either the author of the
            assertion whose conflict-routing auto-detected it, or the
            author of an explicit flag_contradiction() call. None only for
            contradictions persisted before this field existed.
        resolved_by: Principal ID who resolved it (if resolved)
        resolved_at: When it was resolved (if resolved)
        metadata: Open JSON blob for future extension
    """

    id: str
    namespace: str
    subject: str
    predicate: str
    state: ContradictionState = "open"
    member_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    raised_by: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)


def safe_rationale_history(metadata: dict[str, Any]) -> list[dict[str, str]]:
    """Defensively coerce ``metadata["rationale_history"]`` (KI-071) into a
    list of well-formed ``{"rationale", "actor", "at"}`` string dicts.

    ``metadata`` is an open, schema-less blob (ADR-0041) — nothing enforces
    that ``rationale_history`` is a list, that its entries are dicts, or
    that those dicts carry all three keys with string values. Every read
    surface that projects individual entries out of it (KI-075: GraphQL's
    ``ContradictionType``, the CLI's ``contradiction`` sub-app; KI-076:
    both of MCP's ``ontolith.list_contradictions`` and
    ``ontolith.flag_contradiction``) needs the same defense against a
    malformed or legacy-shape blob, so it lives here once rather than
    duplicated per interface. A malformed entry degrades to blank fields
    rather than raising — critically, this must not raise: an uncaught
    exception from one bad contradiction previously took down GraphQL's
    entire ``Query.contradictions`` list, not just the one contradiction
    it belonged to (KI-075 review, round 2).

    One caller isn't a read surface at all: ``Ontology.flag_contradiction()``'s
    "extend" branch also reads prior ``rationale_history`` through this
    function, before appending a new entry and persisting the result (KI-076
    review, round 2) — a malformed prior blob there previously either raised
    or, worse, got silently corrupted further on write (e.g. a bare string
    exploding into one list entry per character). Unlike the read surfaces,
    where coercion is a per-request projection that leaves the stored
    ``metadata`` untouched, this call's coerced result IS what gets
    persisted — any content this function couldn't parse is dropped for
    good, not just hidden from that one response. Accepted as the least-bad
    option: raising would still block a legitimate rationale from being
    recorded, and silently corrupting further (the pre-fix behavior) is
    strictly worse than losing unparseable history.

    REST alone deliberately does NOT go through this: its
    ``ContradictionOut.metadata`` field returns the raw, unprojected blob
    (any shape is valid JSON) rather than a ``rationale_history``-specific
    view, so it never indexes into an individual entry and can't raise on
    a malformed one.
    """
    raw = metadata.get("rationale_history", [])
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        result.append(
            {
                "rationale": str(entry.get("rationale") or ""),
                "actor": str(entry.get("actor") or ""),
                "at": str(entry.get("at") or ""),
            }
        )
    return result


__all__ = ["Contradiction", "ContradictionState", "safe_rationale_history"]
