"""Conformance vectors for SPEC §10 conflict routing.

Tests the pure routing logic (govern/conflict.py) and end-to-end behaviour
through the storage layer. All tests use injected clocks and IDs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ontolith import Ontology
from ontolith.core import Assertion, FixedClock, FixedIdProvider
from ontolith.govern.conflict import Activate, Contradict, Supersede, route

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

T0 = datetime(2025, 1, 1, tzinfo=UTC)
T1 = datetime(2025, 6, 1, tzinfo=UTC)
T2 = datetime(2025, 12, 1, tzinfo=UTC)
AUTHOR = "alice@example.com"


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


def _kb(tmp_path: Path) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider(
        ["p-0", "e-1", "a-1", "a-2", "a-3", "a-4", "a-5", "contra-1", "prop-1", "prop-2"]
    )
    kb = Ontology.connect(tmp_path / "test.db", clock=clock, id_provider=ids)
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
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


# ===========================================================================
# End-to-end: static contradiction (through storage)
# ===========================================================================


class TestStaticContradiction:
    """SPEC §10.3 — static facts flagged and routed to review."""

    def test_first_assertion_activates(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        proposal, decision = kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        assert proposal.state == "auto_accepted"
        active = kb.assertions(subject=entity.id, predicate="Person.name")
        assert len(active) == 1
        assert active[0].value == "Ada"
        assert active[0].status == "active"

    def test_corroborating_assertion_both_active(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 2  # corroboration: both retained

    def test_conflicting_assertion_both_flagged(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
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

    def test_contradiction_object_created(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Ava", "Text", AUTHOR)

        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None
        assert contradiction.state == "open"
        assert len(contradiction.member_ids) == 2

    def test_third_conflicting_assertion_extends_contradiction(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Ava", "Text", AUTHOR)
        kb.propose(entity.id, "Person.name", "Eve", "Text", AUTHOR)

        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None
        assert len(contradiction.member_ids) == 3

    def test_static_fact_never_silently_overwritten(self, tmp_path: Path) -> None:
        """Original value must still be in storage after a conflict."""
        kb = _kb(tmp_path)
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

    def test_superseded_assertion_window_closed(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(
            entity.id, "Person.employer", "Acme Corp", "Text", AUTHOR, temporality="time_varying"
        )

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=180)

        kb.propose(
            entity.id, "Person.employer", "Beta Inc", "Text", AUTHOR, temporality="time_varying"
        )

        superseded = kb.assertions(
            subject=entity.id, predicate="Person.employer", status="superseded"
        )
        assert len(superseded) == 1
        assert superseded[0].value == "Acme Corp"
        assert superseded[0].valid_to is not None

        active = kb.assertions(subject=entity.id, predicate="Person.employer", status="active")
        assert len(active) == 1
        assert active[0].value == "Beta Inc"

    def test_supersession_chain_links_successor(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(
            entity.id, "Person.employer", "Acme Corp", "Text", AUTHOR, temporality="time_varying"
        )

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=180)

        kb.propose(
            entity.id, "Person.employer", "Beta Inc", "Text", AUTHOR, temporality="time_varying"
        )

        active = kb.assertions(subject=entity.id, predicate="Person.employer", status="active")
        assert len(active) == 1
        assert active[0].supersedes is not None

    def test_non_overlapping_windows_coexist(self, tmp_path: Path) -> None:
        """Employment history: two non-overlapping windows can coexist as 'active'."""
        kb = _kb(tmp_path)
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

    def test_supersession_no_contradiction_object_created(self, tmp_path: Path) -> None:
        """time_varying supersession must NOT create a contradiction."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.propose(
            entity.id, "Person.employer", "Acme Corp", "Text", AUTHOR, temporality="time_varying"
        )

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=180)

        kb.propose(
            entity.id, "Person.employer", "Beta Inc", "Text", AUTHOR, temporality="time_varying"
        )

        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.employer")
        assert contradiction is None
