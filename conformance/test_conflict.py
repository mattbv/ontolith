"""Conformance vectors for SPEC §10 conflict routing.

Tests the pure routing logic (govern/conflict.py) and end-to-end behaviour
through the storage layer. All tests use injected clocks and IDs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import Assertion, FixedClock, FixedIdProvider
from ontolith.core.errors import ValidationError
from ontolith.govern.conflict import Activate, Contradict, Supersede, route
from ontolith.schema import ConceptDef, PropertyDef, RelationDef, SchemaIR

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

T0 = datetime(2025, 1, 1, tzinfo=UTC)
T1 = datetime(2025, 6, 1, tzinfo=UTC)
T2 = datetime(2025, 12, 1, tzinfo=UTC)
AUTHOR = "alice@example.com"
ADMIN = "admin@example.com"


def _assertion(
    id: str,
    value: str,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    status: str = "active",
) -> Assertion:
    return Assertion(
        id=id,
        namespace="default",
        subject="entity-1",
        predicate="Person.name",
        value_kind="literal",
        value_type="Text",
        value=value,
        author=AUTHOR,
        asserted_at=T0,
        valid_from=valid_from,
        valid_to=valid_to,
        status=status,
    )


def _kb(make_kb: KbFactory) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider(
        ["p-0", "e-1", "a-1", "a-2", "a-3", "a-4", "a-5", "contra-1", "prop-1", "prop-2"]
    )
    kb = make_kb(clock, ids)
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
    # Person.employer is declared time_varying so conflict routing (schema-derived
    # per SPEC §10.1) exercises supersession; Person.name defaults to static.
    # Person.phone is static + cardinality="many" (ADR-0017). Person.manager is a
    # relation (not a property) so assert_ref/propose_ref tests have a
    # genuinely relation-declared predicate to target (KI-040's kind check
    # rejects a ref write against a property-declared predicate like
    # employer/name).
    schema = SchemaIR(
        namespace="default",
        version=1,
        concepts={
            "Person": ConceptDef(
                name="Person",
                properties={
                    "name": PropertyDef(name="name", value_type="Text"),
                    "employer": PropertyDef(
                        name="employer", value_type="Text", temporality="time_varying"
                    ),
                    "phone": PropertyDef(name="phone", value_type="Text", cardinality="many"),
                },
                relations={
                    "manager": RelationDef(name="manager", target_concept="Person"),
                },
            ),
        },
    )
    kb.apply_schema(schema, author=ADMIN)
    return kb


# ===========================================================================
# Pure routing unit tests (govern/conflict.py — no storage)
# ===========================================================================


class TestPureRouting:
    """Pure function tests — no storage, no side effects."""

    def test_no_existing_returns_activate(self) -> None:
        incoming = _assertion("a-new", "Ada")
        result = route(incoming, existing=[], temporality="static")
        assert isinstance(result, Activate)

    def test_static_same_value_corroboration_is_activate(self) -> None:
        existing = [_assertion("a-1", "Ada")]
        incoming = _assertion("a-2", "Ada")
        result = route(incoming, existing=existing, temporality="static")
        assert isinstance(result, Activate)

    def test_static_different_value_is_contradict(self) -> None:
        existing = [_assertion("a-1", "Ada")]
        incoming = _assertion("a-2", "Ava")
        result = route(incoming, existing=existing, temporality="static")
        assert isinstance(result, Contradict)
        assert "a-1" in result.member_ids
        assert "a-2" in result.member_ids

    def test_static_contradict_links_existing_contradiction(self) -> None:
        existing = [_assertion("a-1", "Ada")]
        incoming = _assertion("a-2", "Ava")
        result = route(
            incoming, existing=existing, temporality="static", existing_contradiction_id="contra-99"
        )
        assert isinstance(result, Contradict)
        assert result.existing_contradiction_id == "contra-99"

    def test_time_varying_overlapping_different_value_supersedes(self) -> None:
        existing = [_assertion("a-1", "Acme Corp", valid_from=T0)]
        incoming = _assertion("a-2", "Beta Inc", valid_from=T1)
        result = route(incoming, existing=existing, temporality="time_varying")
        assert isinstance(result, Supersede)
        assert "a-1" in result.targets

    def test_time_varying_same_value_no_supersession(self) -> None:
        existing = [_assertion("a-1", "Acme Corp", valid_from=T0)]
        incoming = _assertion("a-2", "Acme Corp", valid_from=T1)
        result = route(incoming, existing=existing, temporality="time_varying")
        assert isinstance(result, Activate)

    def test_time_varying_non_overlapping_windows_coexist(self) -> None:
        """Old window [T0, T1) and new window [T1, ∞) don't overlap → Activate."""
        existing = [_assertion("a-1", "Acme Corp", valid_from=T0, valid_to=T1)]
        incoming = _assertion("a-2", "Beta Inc", valid_from=T1)
        result = route(incoming, existing=existing, temporality="time_varying")
        assert isinstance(result, Activate)

    def test_time_varying_both_open_windows_overlap(self) -> None:
        """Two open windows [T0, ∞) and [T1, ∞) overlap → Supersede."""
        existing = [_assertion("a-1", "Acme Corp", valid_from=T0)]
        incoming = _assertion("a-2", "Beta Inc", valid_from=T1)
        result = route(incoming, existing=existing, temporality="time_varying")
        assert isinstance(result, Supersede)

    def test_time_varying_multiple_overlapping_all_superseded(self) -> None:
        existing = [
            _assertion("a-1", "Acme Corp", valid_from=T0),
            _assertion("a-2", "Beta Inc", valid_from=T0),
        ]
        incoming = _assertion("a-3", "Gamma Ltd", valid_from=T1)
        result = route(incoming, existing=existing, temporality="time_varying")
        assert isinstance(result, Supersede)
        assert set(result.targets) == {"a-1", "a-2"}

    def test_time_varying_incoming_closed_window_before_existing_starts(self) -> None:
        """Incoming [T0, T1) ends before existing [T2, ∞) starts → no overlap → Activate."""
        existing = [_assertion("a-1", "Acme Corp", valid_from=T2)]
        incoming = _assertion("a-2", "Beta Inc", valid_from=T0, valid_to=T1)
        result = route(incoming, existing=existing, temporality="time_varying")
        assert isinstance(result, Activate)


# ===========================================================================
# End-to-end: static contradiction (through storage)
# ===========================================================================


class TestStaticContradiction:
    """SPEC §10.3 — static facts flagged and routed to review."""

    def test_first_assertion_activates(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        proposal, decision = kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        assert proposal.state == "auto_accepted"
        active = kb.assertions(subject=entity.id, predicate="Person.name")
        assert len(active) == 1
        assert active[0].value == "Ada"
        assert active[0].status == "active"

    def test_corroborating_assertion_both_active(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 2  # corroboration: both retained

    def test_conflicting_assertion_both_flagged(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Ava", "Text", AUTHOR)

        # Default query excludes flagged
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active == []

        # Flagged assertions are queryable for audit
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert len(flagged) == 2
        assert {a.value for a in flagged} == {"Ada", "Ava"}

    def test_contradiction_object_created(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Ava", "Text", AUTHOR)

        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None
        assert contradiction.state == "open"
        assert len(contradiction.member_ids) == 2

    def test_third_conflicting_assertion_extends_contradiction(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Ava", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Eve", "Text", AUTHOR)

        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None
        assert len(contradiction.member_ids) == 3

    def test_static_fact_never_silently_overwritten(self, make_kb: KbFactory) -> None:
        """Original value must still be in storage after a conflict."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Ava", "Text", AUTHOR)

        all_assertions = kb.assertions(subject=entity.id, predicate="Person.name", status=None)
        values = {a.value for a in all_assertions}
        assert "Ada" in values
        assert "Ava" in values


# ===========================================================================
# End-to-end: time_varying supersession (through storage)
# ===========================================================================


class TestTemporalSupersession:
    """SPEC §10.2 — time_varying properties use temporal supersession."""

    def test_superseded_assertion_window_closed(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.employer", "Acme Corp", "Text", AUTHOR)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=180)

        kb.propose(entity.id, "Person.employer", "Beta Inc", "Text", AUTHOR)

        superseded = kb.assertions(
            subject=entity.id, predicate="Person.employer", status="superseded"
        )
        assert len(superseded) == 1
        assert superseded[0].value == "Acme Corp"
        assert superseded[0].valid_to is not None

        active = kb.assertions(subject=entity.id, predicate="Person.employer", status="active")
        assert len(active) == 1
        assert active[0].value == "Beta Inc"

    def test_supersession_chain_links_successor(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.employer", "Acme Corp", "Text", AUTHOR)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=180)

        kb.propose(entity.id, "Person.employer", "Beta Inc", "Text", AUTHOR)

        active = kb.assertions(subject=entity.id, predicate="Person.employer", status="active")
        assert len(active) == 1
        assert active[0].supersedes is not None

    def test_non_overlapping_windows_coexist(self, make_kb: KbFactory) -> None:
        """Employment history: two non-overlapping windows can coexist as 'active'."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)

        # First employment: [T0, T1)
        a1 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.employer",
            value_kind="literal",
            value_type="Text",
            value="Acme Corp",
            author=AUTHOR,
            asserted_at=T0,
            valid_from=T0,
            valid_to=T1,
        )
        kb.backend.put_assertion(a1)

        # Second employment: [T2, ∞) — non-overlapping
        a2 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.employer",
            value_kind="literal",
            value_type="Text",
            value="Beta Inc",
            author=AUTHOR,
            asserted_at=T2,
            valid_from=T2,
        )
        kb.backend.put_assertion(a2)

        active = kb.assertions(subject=entity.id, predicate="Person.employer", status="active")
        assert len(active) == 2

    def test_multi_target_supersession_full_predecessor_set_recoverable(
        self, make_kb: KbFactory
    ) -> None:
        """KI-008: Assertion.supersedes only records the first predecessor when
        one incoming assertion supersedes several concurrently-overlapping ones
        (e.g. a data-entry race left two employers active at once) — the full
        set must still be recoverable via the assertion_event audit log.
        """
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)

        # Two concurrent, overlapping "active" windows — a data-entry race, not
        # reachable through normal propose() calls (which supersede one at a
        # time), so seeded directly into storage.
        a1 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.employer",
            value_kind="literal",
            value_type="Text",
            value="Acme Corp",
            author=AUTHOR,
            asserted_at=T0,
            valid_from=T0,
        )
        kb.backend.put_assertion(a1)

        a2 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.employer",
            value_kind="literal",
            value_type="Text",
            value="Beta Inc",
            author=AUTHOR,
            asserted_at=T0,
            valid_from=T0,
        )
        kb.backend.put_assertion(a2)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=180)

        kb.propose(entity.id, "Person.employer", "Gamma Ltd", "Text", AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.employer", status="active")
        assert len(active) == 1
        successor = active[0]

        # Assertion.supersedes still records only one predecessor (unchanged,
        # documented v1 shape) ...
        assert successor.supersedes in {a1.id, a2.id}

        # ... but the full predecessor set is recoverable from the event log.
        events = kb.backend.get_assertion_events_by_successor(successor.id)
        assert {e.assertion_id for e in events} == {a1.id, a2.id}
        assert all(e.action == "superseded" for e in events)
        assert all(e.successor_id == successor.id for e in events)

        superseded = kb.assertions(
            subject=entity.id, predicate="Person.employer", status="superseded"
        )
        assert {a.id for a in superseded} == {a1.id, a2.id}

    def test_supersession_no_contradiction_object_created(self, make_kb: KbFactory) -> None:
        """time_varying supersession must NOT create a contradiction."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.employer", "Acme Corp", "Text", AUTHOR)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=180)

        kb.propose(entity.id, "Person.employer", "Beta Inc", "Text", AUTHOR)

        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.employer")
        assert contradiction is None


# ===========================================================================
# Schema-derived temporality (SPEC §10.1 — never caller-supplied)
# ===========================================================================


class TestSchemaDerivedTemporality:
    """propose() resolves temporality from the active schema, not a caller arg."""

    def test_no_schema_registered_falls_back_to_static(self, make_kb: KbFactory) -> None:
        """No schema in the namespace: propose() must still route (default static),
        not raise, and conflicting values must produce a contradiction."""
        clock = FixedClock(T0)
        ids = FixedIdProvider(["e-1", "a-1", "a-2", "contra-1", "prop-1", "prop-2"])
        kb = make_kb(clock, ids)
        kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
        entity = kb.create_entity("Person", author=AUTHOR)

        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Ava", "Text", AUTHOR)

        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

    def test_schema_declared_static_property_contradicts(self, make_kb: KbFactory) -> None:
        """Person.name has no explicit temporality in the schema (defaults to static)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Ava", "Text", AUTHOR)

        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

    def test_schema_declared_time_varying_property_supersedes_with_no_caller_override(
        self, make_kb: KbFactory
    ) -> None:
        """Person.employer is declared time_varying in the schema; propose() no longer
        accepts a temporality kwarg at all, so this exercises the schema-only path."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.employer", "Acme Corp", "Text", AUTHOR)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=180)

        kb.propose(entity.id, "Person.employer", "Beta Inc", "Text", AUTHOR)

        superseded = kb.assertions(
            subject=entity.id, predicate="Person.employer", status="superseded"
        )
        assert len(superseded) == 1
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.employer")
        assert contradiction is None


# ===========================================================================
# Cardinality-aware static routing (ADR-0017)
# ===========================================================================


class TestCardinalityAwareRouting:
    """cardinality="many" static properties coexist on a differing value
    instead of contradicting; cardinality="single" (default) is unchanged."""

    def test_many_cardinality_differing_values_coexist(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.phone", "555-0100", "Text", AUTHOR)
        kb.propose(entity.id, "Person.phone", "555-0200", "Text", AUTHOR)

        active = kb.assertions(subject=entity.id, predicate="Person.phone", status="active")
        assert {a.value for a in active} == {"555-0100", "555-0200"}
        assert kb.backend.get_open_contradiction("default", entity.id, "Person.phone") is None

    def test_single_cardinality_default_still_contradicts(self, make_kb: KbFactory) -> None:
        """Regression guard: cardinality="single" (Person.name's default)
        must still contradict on a differing value, unaffected by ADR-0017."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Ava", "Text", AUTHOR)

        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active == []
        assert kb.backend.get_open_contradiction("default", entity.id, "Person.name") is not None

    def test_explicit_flag_contradiction_overrides_many_cardinality_coexistence(
        self, make_kb: KbFactory
    ) -> None:
        """ADR-0017: flag_contradiction() is an explicit human dispute, not
        schema-driven inference - it deliberately does not consult
        cardinality. Once a contradiction is manually opened on a
        many-cardinality predicate, subsequent same-predicate assertions
        are still swept in for review (the existing "extend open
        contradiction" behavior), not silently activated."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(entity.id, "Person.phone", "555-0100", "Text", AUTHOR)
        b = kb.assert_literal(entity.id, "Person.phone", "555-0200", "Text", AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.phone", status="active")
        assert {x.value for x in active} == {"555-0100", "555-0200"}  # normal many-coexistence

        # A reviewer explicitly disputes these two (e.g. suspected duplicate/typo).
        kb.flag_contradiction(a.id, b.id, AUTHOR)
        assert kb.backend.get_open_contradiction("default", entity.id, "Person.phone") is not None

        # A third, otherwise-legitimate phone number is swept into the
        # dispute rather than silently coexisting.
        kb.assert_literal(entity.id, "Person.phone", "555-0300", "Text", AUTHOR)
        active_after = kb.assertions(subject=entity.id, predicate="Person.phone", status="active")
        assert active_after == []
        flagged = kb.assertions(subject=entity.id, predicate="Person.phone", status="flagged")
        assert {x.value for x in flagged} == {"555-0100", "555-0200", "555-0300"}


# ===========================================================================
# Static routing respects validity windows (SPEC §10.1)
# ===========================================================================


class TestStaticRoutingRespectsWindows:
    """A static value true only in a disjoint, already-closed window is not
    in conflict with a differing value true now — SPEC §10.1's overlap
    formula applies to the static branch, not just time_varying."""

    def test_non_overlapping_static_windows_do_not_contradict(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        # Historical, already-closed record: true only in [T0, T1).
        kb.assert_literal(
            entity.id, "Person.name", "Old Name", "Text", AUTHOR, valid_from=T0, valid_to=T1
        )
        # Current record: true from T1 onward - disjoint window, no overlap.
        kb.assert_literal(entity.id, "Person.name", "New Name", "Text", AUTHOR, valid_from=T1)

        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert {a.value for a in active} == {"Old Name", "New Name"}
        assert kb.backend.get_open_contradiction("default", entity.id, "Person.name") is None

    def test_overlapping_static_windows_still_contradict(self, make_kb: KbFactory) -> None:
        """Regression guard: overlapping windows (the common case — both
        default to an open window from asserted_at) still contradict."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)

        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active == []
        assert kb.backend.get_open_contradiction("default", entity.id, "Person.name") is not None


# ===========================================================================
# Unknown predicate rejected at write time (SPEC §4)
# ===========================================================================


class TestUnknownPredicateRejected:
    def test_propose_unknown_predicate_raises(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        with pytest.raises(ValidationError, match="Unknown predicate"):
            kb.propose(entity.id, "Person.nmae", "Ada", "Text", AUTHOR)

    def test_assert_literal_unknown_predicate_raises(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        with pytest.raises(ValidationError, match="Unknown predicate"):
            kb.assert_literal(entity.id, "Person.nmae", "Ada", "Text", AUTHOR)

    def test_unknown_predicate_permitted_without_a_registered_schema(
        self, make_kb: KbFactory
    ) -> None:
        """No schema in the namespace: nothing to validate a predicate
        against, so any predicate is accepted (falls back to static)."""
        clock = FixedClock(T0)
        ids = FixedIdProvider(["p-0", "e-1", "a-1"])
        kb = make_kb(clock, ids)
        kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
        entity = kb.create_entity("Person", author=AUTHOR)

        assertion = kb.assert_literal(entity.id, "Person.whatever", "Ada", "Text", AUTHOR)
        assert assertion.value == "Ada"


# ===========================================================================
# value_type mismatch rejected at write time (SPEC §4, KI-031, ADR-0028)
# ===========================================================================


class TestValueTypeMismatchRejected:
    def test_propose_value_type_mismatch_raises(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        with pytest.raises(ValidationError, match="declared value_type"):
            kb.propose(entity.id, "Person.name", "42", "Integer", AUTHOR)

    def test_assert_literal_value_type_mismatch_raises(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        with pytest.raises(ValidationError, match="declared value_type"):
            kb.assert_literal(entity.id, "Person.name", "42", "Integer", AUTHOR)

    def test_assert_literal_matching_value_type_succeeds(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        assert assertion.value_type == "Text"

    def test_value_type_mismatch_permitted_without_a_registered_schema(
        self, make_kb: KbFactory
    ) -> None:
        """No schema in the namespace: nothing to validate value_type
        against, so any value_type is accepted."""
        clock = FixedClock(T0)
        ids = FixedIdProvider(["p-0", "e-1", "a-1"])
        kb = make_kb(clock, ids)
        kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
        entity = kb.create_entity("Person", author=AUTHOR)

        assertion = kb.assert_literal(entity.id, "Person.age", "42", "Integer", AUTHOR)
        assert assertion.value_type == "Integer"

    def test_literal_under_relation_predicate_is_a_kind_error_not_a_value_type_error(
        self, make_kb: KbFactory
    ) -> None:
        """A predicate declared as a relation has no PropertyDef.value_type
        to compare against — value_type_of() returns None for it, so the
        value_type-mismatch check above is a no-op here specifically.
        That's not a gap anymore, though: a literal assertion under a
        relation-declared predicate is now rejected by a dedicated kind
        check instead (KI-040), exercised in TestPredicateKindMismatch
        below — this test only pins that the two checks are independent
        (a literal under a relation predicate is caught by the kind check,
        not misattributed to a "value_type mismatch" error)."""
        clock = FixedClock(T0)
        ids = FixedIdProvider(["p-0", "e-1", "e-2", "a-1"])
        kb = make_kb(clock, ids)
        kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
        kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    relations={
                        "employer": RelationDef(name="employer", target_concept="Organization"),
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )
        kb.apply_schema(schema, author=ADMIN)
        entity = kb.create_entity("Person", author=AUTHOR)

        with pytest.raises(ValidationError, match="declared a relation") as exc_info:
            kb.assert_literal(entity.id, "Person.employer", "Acme Corp", "Text", AUTHOR)
        assert "value_type" not in str(exc_info.value)


class TestPredicateKindMismatch:
    """KI-040: a write's kind (literal vs. ref) must match the predicate's
    schema-declared kind (property vs. relation)."""

    def _kb_with_property_and_relation(self, make_kb: KbFactory) -> Ontology:
        clock = FixedClock(T0)
        ids = FixedIdProvider([f"id-{i}" for i in range(20)])
        kb = make_kb(clock, ids)
        kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
        kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={"name": PropertyDef(name="name", value_type="Text")},
                    relations={
                        "employer": RelationDef(name="employer", target_concept="Organization"),
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )
        kb.apply_schema(schema, author=ADMIN)
        return kb

    def test_assert_literal_against_relation_predicate_raises(self, make_kb: KbFactory) -> None:
        kb = self._kb_with_property_and_relation(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)

        with pytest.raises(ValidationError, match="declared a relation"):
            kb.assert_literal(entity.id, "Person.employer", "Acme Corp", "Text", AUTHOR)

    def test_assert_ref_against_property_predicate_raises(self, make_kb: KbFactory) -> None:
        kb = self._kb_with_property_and_relation(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        target = kb.create_entity("Organization", author=AUTHOR)

        with pytest.raises(ValidationError, match="declared a property"):
            kb.assert_ref(entity.id, "Person.name", target.id, AUTHOR)

    def test_propose_against_relation_predicate_raises(self, make_kb: KbFactory) -> None:
        kb = self._kb_with_property_and_relation(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)

        with pytest.raises(ValidationError, match="declared a relation"):
            kb.propose(entity.id, "Person.employer", "Acme Corp", "Text", AUTHOR)

    def test_rejected_propose_leaves_no_proposal_row(self, make_kb: KbFactory) -> None:
        """The kind check runs before any Proposal is constructed or
        persisted - a rejected propose()/propose_ref() must leave no
        trace, the same way an unknown-predicate or value_type-mismatch
        rejection already does."""
        kb = self._kb_with_property_and_relation(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        target = kb.create_entity("Organization", author=AUTHOR)

        with pytest.raises(ValidationError):
            kb.propose(entity.id, "Person.employer", "Acme Corp", "Text", AUTHOR)
        with pytest.raises(ValidationError):
            kb.propose_ref(entity.id, "Person.name", target.id, AUTHOR)

        assert kb.proposals(state=None) == []

    def test_propose_ref_against_property_predicate_raises(self, make_kb: KbFactory) -> None:
        kb = self._kb_with_property_and_relation(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        target = kb.create_entity("Organization", author=AUTHOR)

        with pytest.raises(ValidationError, match="declared a property"):
            kb.propose_ref(entity.id, "Person.name", target.id, AUTHOR)

    def test_matching_kind_still_succeeds(self, make_kb: KbFactory) -> None:
        """Regression guard: the kind check must not reject correctly
        -kinded writes."""
        kb = self._kb_with_property_and_relation(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        target = kb.create_entity("Organization", author=AUTHOR)

        literal = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        assert literal.value == "Ada"
        ref = kb.assert_ref(entity.id, "Person.employer", target.id, AUTHOR)
        assert ref.value == target.id

    def test_permitted_without_a_registered_schema(self, make_kb: KbFactory) -> None:
        """No schema in the namespace: nothing to validate kind against, so
        any predicate/write-kind combination is accepted - matching the
        existing no-schema precedent for value_type/unknown-predicate
        checks."""
        clock = FixedClock(T0)
        ids = FixedIdProvider(["p-0", "e-1", "e-2", "a-1"])
        kb = make_kb(clock, ids)
        kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
        entity = kb.create_entity("Person", author=AUTHOR)
        target = kb.create_entity("Organization", author=AUTHOR)

        assertion = kb.assert_literal(entity.id, "Person.employer", "Acme Corp", "Text", AUTHOR)
        assert assertion.value == "Acme Corp"
        ref = kb.assert_ref(entity.id, "Person.name", target.id, AUTHOR)
        assert ref.value == target.id


# ===========================================================================
# Explicit valid_from/valid_to on the governed write API
# ===========================================================================


class TestValidityWindowWriteAPI:
    """assert_literal/assert_ref/propose/propose_ref accept valid_from/
    valid_to (SPEC §5.3) - previously only reachable by bypassing
    governance entirely (direct backend.put_assertion)."""

    def test_assert_literal_explicit_window_persisted(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        assertion = kb.assert_literal(
            entity.id, "Person.name", "Old Name", "Text", AUTHOR, valid_from=T0, valid_to=T1
        )
        assert assertion.valid_from == T0
        assert assertion.valid_to == T1

    def test_propose_auto_accepted_explicit_window_persisted(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        proposal, decision = kb.propose(
            entity.id, "Person.name", "Old Name", "Text", AUTHOR, valid_from=T0, valid_to=T1
        )
        assert proposal.state == "auto_accepted"
        [assertion] = kb.assertions(subject=entity.id, predicate="Person.name", status=None)
        assert assertion.valid_from == T0
        assert assertion.valid_to == T1

    def test_assert_ref_explicit_window_persisted(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        target = kb.create_entity("Person", author=AUTHOR)
        assertion = kb.assert_ref(
            entity.id, "Person.manager", target.id, AUTHOR, valid_from=T0, valid_to=T1
        )
        assert assertion.valid_from == T0
        assert assertion.valid_to == T1

    def test_propose_ref_reviewed_explicit_window_survives_replay(self, make_kb: KbFactory) -> None:
        """Mirrors test_propose_reviewed_explicit_window_survives_replay for
        propose_ref()'s replay path, not just propose()'s."""
        kb = _kb(make_kb)
        kb.create_principal(
            "bot@example.com", kind="ai", owner=AUTHOR, default_capability="propose"
        )
        kb.create_principal(
            "reviewer@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=AUTHOR)
        target = kb.create_entity("Person", author=AUTHOR)
        proposal, decision = kb.propose_ref(
            entity.id,
            "Person.manager",
            target.id,
            "bot@example.com",
            model="test-model-v1",
            valid_from=T0,
            valid_to=T1,
        )
        assert proposal.state == "require_review"
        kb.accept_proposal(proposal.id, "reviewer@example.com")

        [assertion] = kb.assertions(subject=entity.id, predicate="Person.manager", status=None)
        assert assertion.valid_from == T0
        assert assertion.valid_to == T1

    def test_propose_reviewed_explicit_window_survives_replay(self, make_kb: KbFactory) -> None:
        """A require_review proposal's requested window must survive
        accept_proposal's operation replay, not just the auto-accept path."""
        kb = _kb(make_kb)
        kb.create_principal(
            "bot@example.com", kind="ai", owner=AUTHOR, default_capability="propose"
        )
        kb.create_principal(
            "reviewer@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=AUTHOR)
        proposal, decision = kb.propose(
            entity.id,
            "Person.name",
            "Old Name",
            "Text",
            "bot@example.com",
            model="test-model-v1",
            valid_from=T0,
            valid_to=T1,
        )
        assert proposal.state == "require_review"
        kb.accept_proposal(proposal.id, "reviewer@example.com")

        [assertion] = kb.assertions(subject=entity.id, predicate="Person.name", status=None)
        assert assertion.valid_from == T0
        assert assertion.valid_to == T1

    def test_default_window_unchanged_when_not_specified(self, make_kb: KbFactory) -> None:
        """Regression guard: omitting valid_from/valid_to keeps the existing
        default behavior (valid_from defaults to asserted_at, valid_to open)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        assert assertion.valid_from == assertion.asserted_at
        assert assertion.valid_to is None
