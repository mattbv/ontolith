"""Conflict routing — pure functions implementing SPEC §10.

Routing by temporality:
  time_varying  → temporal supersession (§10.2): expected change, no review
  static        → contradiction (§10.3): sources disagree, route to review

This module is PURE: no I/O, no imports from store, deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ontolith.core import Assertion


# ---------------------------------------------------------------------------
# Result types — what the caller must do after routing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Activate:
    """No conflict: persist the incoming assertion as-is."""

    pass


@dataclass(frozen=True)
class Supersede:
    """Close the validity window on each existing assertion and activate incoming.

    targets: assertion IDs whose valid_to must be set to incoming.valid_from
             (or asserted_at when valid_from is None).
    """

    targets: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Contradict:
    """Flag all members and open (or extend) a contradiction for review.

    member_ids: existing + incoming assertion IDs to flag as 'flagged'.
    existing_contradiction_id: if an open contradiction already exists, extend it;
                               otherwise the caller creates a new one.
    """

    member_ids: list[str] = field(default_factory=list)
    existing_contradiction_id: str | None = None


ConflictResult = Activate | Supersede | Contradict


# ---------------------------------------------------------------------------
# Routing entry point
# ---------------------------------------------------------------------------


def route(
    incoming: Assertion,
    existing: list[Assertion],
    temporality: Literal["static", "time_varying"],
    existing_contradiction_id: str | None = None,
    cardinality: Literal["single", "many"] = "single",
    supersedes_hint: str | None = None,
) -> ConflictResult:
    """Determine how to handle incoming vs existing assertions (SPEC §10).

    Args:
        incoming: The assertion being proposed.
        existing: Currently active assertions on the same (subject, predicate).
        temporality: Schema-declared temporality for this predicate.
        existing_contradiction_id: ID of an open Contradiction for this
            (subject, predicate), if one already exists.
        cardinality: Schema-declared cardinality for this predicate.
            "many" static properties coexist on a differing value instead
            of contradicting (ADR-0017). For "many" time_varying properties,
            a differing overlapping-window value coexists too, unless
            supersedes_hint says otherwise (ADR-0050). Not consulted for
            "single" time_varying properties — window overlap and a
            differing value are already unambiguous there.
        supersedes_hint: id of a specific existing assertion this incoming
            one explicitly replaces (ADR-0050). Only meaningful for
            cardinality="many" time_varying properties — every other
            combination ignores it, since the routing there is already
            unambiguous without a hint. The caller (Ontology) is
            responsible for validating the hint refers to a real, active,
            same-(subject, predicate) assertion before calling route();
            this function validates that it names an assertion incoming
            actually overlaps-and-differs-from — see the ValueError below.
            Checked even when `existing` has no *other* candidate
            incoming would otherwise conflict with: an explicit hint is
            never silently dropped just because nothing else happens to
            overlap, including when `existing` is empty outright (e.g. a
            caller retrying a stale hint after its target left the active
            set some other way).

    Returns:
        Activate  — no conflict; caller persists incoming as-is.
        Supersede — caller closes validity windows then activates incoming.
        Contradict — caller flags all members and persists/extends contradiction.

    Raises:
        ValueError: supersedes_hint is set, cardinality is "many", and
            temporality is "time_varying", but the hint does not name an
            assertion incoming actually overlaps-and-differs-from (SPEC
            §10.2's own supersession precondition) — a caller-contract
            violation the Ontology layer is expected to translate into a
            ValidationError at the boundary, not something callers should
            rely on route() to swallow silently. Raised regardless of
            whether any other existing assertion would otherwise route.
    """
    if temporality == "time_varying":
        return _route_time_varying(incoming, existing, cardinality, supersedes_hint)
    else:
        return _route_static(incoming, existing, existing_contradiction_id, cardinality)


def _route_time_varying(
    incoming: Assertion,
    existing: list[Assertion],
    cardinality: Literal["single", "many"] = "single",
    supersedes_hint: str | None = None,
) -> ConflictResult:
    """Temporal supersession (SPEC §10.2), cardinality-aware for "many" (ADR-0050).

    Values MAY coexist if validity windows don't overlap; supersede only when
    an existing window overlaps with the incoming one.

    For cardinality="many", an overlapping differing value no longer
    supersedes automatically — window overlap and a differing value can't
    tell "this replaces my current value" from "this is a new, additional
    concurrent value" apart, unlike "single" where there is only ever one
    logical slot to replace. Without supersedes_hint, every overlapping
    differing value coexists (mirrors _route_static's own "many" branch,
    which never auto-supersedes at all). With supersedes_hint, exactly the
    named assertion is superseded; every other overlapping-differing one
    still coexists untouched.

    The cardinality="many" + supersedes_hint check runs before the
    "nothing overlaps" short-circuit below, deliberately — an explicit
    hint must never be silently dropped just because no *other* candidate
    happens to overlap too (including when `existing` is empty outright).
    A caller who supplies a hint always gets either the supersession they
    asked for or a ValueError explaining why not; never silent no-op.
    """
    overlapping = [
        e for e in existing if _windows_overlap(e, incoming) and e.value != incoming.value
    ]

    if cardinality == "many":
        if supersedes_hint is None:
            return Activate()
        if not any(e.id == supersedes_hint for e in overlapping):
            raise ValueError(
                f"supersedes={supersedes_hint!r} does not name an existing, active "
                "assertion on this (subject, predicate) whose validity window "
                "overlaps the incoming one with a different value — nothing to "
                "supersede"
            )
        return Supersede(targets=[supersedes_hint])

    if not overlapping:
        return Activate()
    return Supersede(targets=[e.id for e in overlapping])


def _route_static(
    incoming: Assertion,
    existing: list[Assertion],
    existing_contradiction_id: str | None,
    cardinality: Literal["single", "many"] = "single",
) -> ConflictResult:
    """Contradiction routing (SPEC §10.3), cardinality-aware (ADR-0017).

    Static facts must never be silently overwritten. For cardinality="single"
    (the default), any value disagreement on an overlapping window flags all
    parties and routes to review. For cardinality="many", a differing value
    coexists instead — the property is schema-declared as legitimately
    multi-valued (e.g. phone numbers), so distinct values aren't in dispute.

    Only assertions whose validity window overlaps the incoming one are
    considered (SPEC §10.1's own formula, mirroring _route_time_varying) —
    a value that was true in a disjoint, already-closed window is not in
    conflict with a value true now.
    """
    conflicting = [
        e for e in existing if _windows_overlap(e, incoming) and e.value != incoming.value
    ]
    if not conflicting:
        # Corroboration: keep both, do NOT merge confidence (v1 rule)
        return Activate()

    if cardinality == "many":
        # Distinct values legitimately coexist for a multi-valued property.
        return Activate()

    member_ids = [e.id for e in conflicting] + [incoming.id]
    return Contradict(
        member_ids=member_ids,
        existing_contradiction_id=existing_contradiction_id,
    )


def _windows_overlap(a: Assertion, b: Assertion) -> bool:
    """Return True if two bitemporal validity windows overlap.

    An open window (valid_to=None) extends to ∞.
    Windows [s1, e1) and [s2, e2) overlap iff s1 < e2 AND s2 < e1.
    """
    # Start of each window (None → epoch, treat as always started)
    a_from = a.valid_from
    b_from = b.valid_from

    # End of each window (None → ∞)
    a_to = a.valid_to
    b_to = b.valid_to

    # [a_from, a_to) overlaps [b_from, b_to)?
    # Two intervals overlap unless one ends before the other starts.
    if a_to is not None and b_from is not None and a_to <= b_from:
        return False
    if b_to is not None and a_from is not None and b_to <= a_from:
        return False
    return True


__all__ = ["Activate", "Supersede", "Contradict", "ConflictResult", "route"]
