"""Conformance vectors for contradiction resolution (SPEC §10.3, §19).

Ontology.resolve_contradiction() lets a reviewer pick a winning assertion
among an open contradiction's flagged members. Losers are retracted, the
winner is reactivated, and the contradiction transitions to 'resolved'.
Resolution is append-only: only status/state fields mutate, nothing is deleted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.core.errors import AuthError, CapabilityError, NotFoundError, ValidationError

T0 = datetime(2025, 1, 1, tzinfo=UTC)

HUMAN_WRITE = "alice@example.com"
REVIEWER = "bob@example.com"
NON_REVIEWER = "carol@example.com"


def _kb(make_kb: KbFactory) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(30)])
    kb = make_kb(clock, ids)
    kb.create_principal(HUMAN_WRITE, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(REVIEWER, kind="human", auth_method="oidc", default_capability="review")
    kb.create_principal(
        NON_REVIEWER, kind="human", auth_method="oidc", default_capability="propose"
    )
    return kb


def _open_contradiction(kb: Ontology, entity_id: str) -> tuple[str, str, str]:
    """Create a two-member contradiction; returns (contradiction_id, ada_id, ava_id)."""
    kb.propose(entity_id, "Person.name", "Ada", "Text", HUMAN_WRITE)
    kb.propose(entity_id, "Person.name", "Ava", "Text", HUMAN_WRITE)
    flagged = kb.assertions(subject=entity_id, predicate="Person.name", status="flagged")
    ada_id = next(a.id for a in flagged if a.value == "Ada")
    ava_id = next(a.id for a in flagged if a.value == "Ava")
    contradiction = kb.backend.get_open_contradiction("default", entity_id, "Person.name")
    assert contradiction is not None
    return contradiction.id, ada_id, ava_id


# ===========================================================================
# raised_by attribution
# ===========================================================================


class TestContradictionRaisedBy:
    """Contradictions record who raised them, whether auto-detected during
    conflict routing or explicitly flagged via flag_contradiction()."""

    def test_auto_detected_contradiction_records_raiser(self, make_kb: KbFactory) -> None:
        """The author of the assertion whose write triggered conflict-routing
        detection is recorded as raised_by."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, _, _ = _open_contradiction(kb, entity.id)

        contradiction = kb.backend.get_contradiction(contradiction_id)
        assert contradiction is not None
        assert contradiction.raised_by == HUMAN_WRITE

    def test_explicit_flag_records_flagging_principal(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.assert_literal(entity.id, "Person.born", "1815", "Text", HUMAN_WRITE)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="propose"
        )
        a = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)
        b = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)

        contradiction, _ = kb.flag_contradiction(a.id, b.id, "dave@example.com")

        fetched = kb.backend.get_contradiction(contradiction.id)
        assert fetched is not None
        assert fetched.raised_by == "dave@example.com"


# ===========================================================================
# Successful resolution
# ===========================================================================


class TestResolveContradiction:
    def test_resolve_sets_contradiction_state(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        resolved = kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        assert resolved.state == "resolved"

    def test_resolve_sets_resolved_by_and_resolved_at(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        resolved = kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        assert resolved.resolved_by == REVIEWER
        assert resolved.resolved_at is not None

    def test_winner_reactivated(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].id == ada_id
        assert active[0].value == "Ada"

    def test_losers_retracted(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        retracted = kb.assertions(subject=entity.id, predicate="Person.name", status="retracted")
        assert len(retracted) == 1
        assert retracted[0].id == ava_id

    def test_losers_no_longer_flagged(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert flagged == []

    def test_resolution_is_append_only(self, make_kb: KbFactory) -> None:
        """Both original assertion records must still exist with their original values."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        all_assertions = kb.assertions(subject=entity.id, predicate="Person.name", status=None)
        by_id = {a.id: a for a in all_assertions}
        assert by_id[ada_id].value == "Ada"
        assert by_id[ava_id].value == "Ava"

    def test_three_member_contradiction_resolves_all_losers(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Eve", "Text", HUMAN_WRITE)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        winner = next(a for a in flagged if a.value == "Ada")
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        kb.resolve_contradiction(contradiction.id, winner.id, REVIEWER)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        retracted = kb.assertions(subject=entity.id, predicate="Person.name", status="retracted")
        assert len(active) == 1
        assert len(retracted) == 2


# ===========================================================================
# Guard rails
# ===========================================================================


class TestResolveContradictionGuards:
    def test_unknown_resolver_raises_auth_error(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        with pytest.raises(AuthError, match="Principal not found"):
            kb.resolve_contradiction(contradiction_id, ada_id, "nobody@example.com")

    def test_non_reviewer_raises_capability_error(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        with pytest.raises(CapabilityError, match="lacks review capability"):
            kb.resolve_contradiction(contradiction_id, ada_id, NON_REVIEWER)

    def test_unknown_contradiction_raises_not_found(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        with pytest.raises(NotFoundError, match="Contradiction not found"):
            kb.resolve_contradiction("nonexistent-id", "some-assertion", REVIEWER)

    def test_winner_not_a_member_raises_validation_error(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, _, _ = _open_contradiction(kb, entity.id)

        with pytest.raises(ValidationError, match="is not a member"):
            kb.resolve_contradiction(contradiction_id, "not-a-real-assertion", REVIEWER)

    def test_already_resolved_contradiction_raises(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)
        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)

        with pytest.raises(ValidationError, match="is not open"):
            kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)

    def test_ai_resolver_raises_capability_error(self, make_kb: KbFactory) -> None:
        """A misconfigured AI principal with review capability must still be
        blocked from resolving contradictions (ADR-0003)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "misconfigured-ai-reviewer",
            kind="ai",
            auth_method="apikey",
            owner=HUMAN_WRITE,
            default_capability="review",
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        with pytest.raises(CapabilityError, match="AI principal and cannot review"):
            kb.resolve_contradiction(contradiction_id, ada_id, "misconfigured-ai-reviewer")
