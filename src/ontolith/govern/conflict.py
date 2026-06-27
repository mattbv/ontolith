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
) -> ConflictResult:
    """Determine how to handle incoming vs existing assertions (SPEC §10).

    Args:
        incoming: The assertion being proposed.
        existing: Currently active assertions on the same (subject, predicate).
        temporality: Schema-declared temporality for this predicate.
        existing_contradiction_id: ID of an open Contradiction for this
            (subject, predicate), if one already exists.

    Returns:
        Activate  — no conflict; caller persists incoming as-is.
        Supersede — caller closes validity windows then activates incoming.
        Contradict — caller flags all members and persists/extends contradiction.
    """
    if not existing:
        return Activate()

    if temporality == "time_varying":
        return _route_time_varying(incoming, existing)
    else:
        return _route_static(incoming, existing, existing_contradiction_id)


def _route_time_varying(incoming: Assertion, existing: list[Assertion]) -> ConflictResult:
    """Temporal supersession (SPEC §10.2).

    Values MAY coexist if validity windows don't overlap; supersede only when
    an existing window overlaps with the incoming one.
    """
    overlapping = [
        e for e in existing if _windows_overlap(e, incoming) and e.value != incoming.value
    ]
    if not overlapping:
        return Activate()
    return Supersede(targets=[e.id for e in overlapping])


def _route_static(
    incoming: Assertion,
    existing: list[Assertion],
    existing_contradiction_id: str | None,
) -> ConflictResult:
    """Contradiction routing (SPEC §10.3).

    Static facts must never be silently overwritten. Any value disagreement
    flags all parties and routes to review.
    """
    conflicting = [e for e in existing if e.value != incoming.value]
    if not conflicting:
        # Corroboration: keep both, do NOT merge confidence (v1 rule)
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
