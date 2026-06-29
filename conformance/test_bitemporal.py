"""Conformance vectors for SPEC §10 bitemporal as_of() time-travel.

Two time dimensions:
  valid_from / valid_to — when the fact was true in the real world
  asserted_at           — when we learned about the fact

An assertion is visible at time t iff:
  valid_from <= t < (valid_to or ∞)  AND  asserted_at <= t

All tests use injected clocks and IDs. Property tests verify reconstruction
against the theoretical filter using Hypothesis.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ontolith import Ontology
from ontolith.core import Assertion, FixedClock, FixedIdProvider

T0 = datetime(2025, 1, 1, tzinfo=UTC)
T1 = datetime(2025, 6, 1, tzinfo=UTC)
T2 = datetime(2025, 12, 1, tzinfo=UTC)
T_BEFORE = datetime(2024, 12, 1, tzinfo=UTC)
AUTHOR = "alice@example.com"


def _kb(tmp_path: Path) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider(
        [f"id-{i}" for i in range(30)]
    )
    kb = Ontology.connect(tmp_path / "test.db", clock=clock, id_provider=ids)
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    return kb


def _put_assertion(kb: Ontology, entity_id: str, value: str, **kwargs: object) -> Assertion:
    """Bypass propose() to insert an assertion with explicit temporal fields."""
    a = Assertion(
        id=kb.id_provider.next(),
        namespace="default",
        subject=entity_id,
        predicate="Person.name",
        value_kind="literal",
        value_type="Text",
        value=value,
        author=AUTHOR,
        asserted_at=kwargs.get("asserted_at", kb.clock.now()),  # type: ignore[arg-type]
        valid_from=kwargs.get("valid_from"),  # type: ignore[arg-type]
        valid_to=kwargs.get("valid_to"),  # type: ignore[arg-type]
        status=str(kwargs.get("status", "active")),
    )
    kb.backend.put_assertion(a)
    return a


# ===========================================================================
# Basic as_of semantics
# ===========================================================================


class TestAsOfBasics:
    """Core bitemporal visibility filter: valid_from <= t < valid_to AND asserted_at <= t"""

    def test_assertion_visible_at_asserted_time(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0)

        visible = kb.as_of(T0).assertions(subject=entity.id)
        assert len(visible) == 1
        assert visible[0].value == "Ada"

    def test_assertion_not_visible_before_asserted(self, tmp_path: Path) -> None:
        """asserted_at = T1; as_of(T0) must not reveal it."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T1)

        assert kb.as_of(T0).assertions(subject=entity.id) == []

    def test_assertion_excluded_when_validity_window_closed(self, tmp_path: Path) -> None:
        """valid_to = T1; as_of(T1) must not show it (half-open interval: t < valid_to)."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=T0, valid_to=T1)

        assert kb.as_of(T1).assertions(subject=entity.id) == []

    def test_assertion_excluded_when_validity_not_yet_started(self, tmp_path: Path) -> None:
        """valid_from = T2; as_of(T1) must not show it."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=T2)

        assert kb.as_of(T1).assertions(subject=entity.id) == []

    def test_assertion_visible_just_before_valid_to(self, tmp_path: Path) -> None:
        """valid_to = T1; as_of(T1 - 1s) is still inside the window."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=T0, valid_to=T1)
        just_before = T1 - timedelta(seconds=1)

        visible = kb.as_of(just_before).assertions(subject=entity.id)
        assert len(visible) == 1

    def test_null_valid_from_treated_as_always_started(self, tmp_path: Path) -> None:
        """valid_from = NULL means open from the beginning; visible as long as asserted_at <= t."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=None)

        visible = kb.as_of(T0).assertions(subject=entity.id)
        assert len(visible) == 1

    def test_string_iso_accepted(self, tmp_path: Path) -> None:
        """as_of() must accept ISO-format strings as well as datetimes."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0)

        visible = kb.as_of(T0.isoformat()).assertions(subject=entity.id)
        assert len(visible) == 1

    def test_open_window_visible_well_into_future(self, tmp_path: Path) -> None:
        """An assertion with valid_to=NULL remains visible at any future t."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=T0)

        assert len(kb.as_of(T2).assertions(subject=entity.id)) == 1

    def test_no_status_filter_applied(self, tmp_path: Path) -> None:
        """as_of() does not filter by current status — temporal dims decide visibility."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        # Insert a 'superseded' assertion that was valid at T0
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=T0, valid_to=T1, status="superseded")

        visible = kb.as_of(T0).assertions(subject=entity.id)
        assert len(visible) == 1
        assert visible[0].status == "superseded"


# ===========================================================================
# Supersession chain time-travel
# ===========================================================================


class TestAsOfSupersession:
    """Time-travel across a supersession chain for time_varying properties."""

    def _setup_supersession(self, tmp_path: Path) -> tuple[Ontology, str]:
        """Returns kb and entity_id with two employments: Acme[T0,T1) → Beta[T1,∞)."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        # Acme: [T0, T1) superseded
        _put_assertion(
            kb, entity.id, "Acme Corp",
            asserted_at=T0, valid_from=T0, valid_to=T1, status="superseded",
        )
        # Beta: [T1, ∞) active, asserted at T1
        _put_assertion(
            kb, entity.id, "Beta Inc",
            asserted_at=T1, valid_from=T1,
        )
        return kb, entity.id

    def test_superseded_assertion_visible_before_supersession(self, tmp_path: Path) -> None:
        kb, eid = self._setup_supersession(tmp_path)
        visible = kb.as_of(T0).assertions(subject=eid, predicate="Person.name")
        assert len(visible) == 1
        assert visible[0].value == "Acme Corp"

    def test_new_assertion_not_visible_before_its_assertion_time(self, tmp_path: Path) -> None:
        kb, eid = self._setup_supersession(tmp_path)
        # Beta was asserted at T1; as_of(T0) must not reveal it
        visible = kb.as_of(T0).assertions(subject=eid, predicate="Person.name")
        assert all(a.value != "Beta Inc" for a in visible)

    def test_as_of_after_supersession_shows_new_assertion(self, tmp_path: Path) -> None:
        kb, eid = self._setup_supersession(tmp_path)
        visible = kb.as_of(T2).assertions(subject=eid, predicate="Person.name")
        assert len(visible) == 1
        assert visible[0].value == "Beta Inc"

    def test_at_transition_time_only_new_assertion_visible(self, tmp_path: Path) -> None:
        """At exactly T1: Acme window is [T0, T1) so T1 is excluded; Beta starts at T1."""
        kb, eid = self._setup_supersession(tmp_path)
        visible = kb.as_of(T1).assertions(subject=eid, predicate="Person.name")
        assert len(visible) == 1
        assert visible[0].value == "Beta Inc"


# ===========================================================================
# Entity-level time-travel via as_of().query()
# ===========================================================================


class TestAsOfQuery:
    """kb.as_of(t).query(concept) reconstructs entity set at time t."""

    def test_entity_not_visible_before_creation(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        # Entity created at T0 (default clock)
        kb.create_entity("Person", author=AUTHOR)

        assert kb.as_of(T_BEFORE).query("Person").all() == []

    def test_entity_visible_at_creation_time(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=AUTHOR)

        assert len(kb.as_of(T0).query("Person").all()) == 1

    def test_query_where_applies_temporal_filter(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        # Assertion recorded at T1, so invisible as_of T0
        _put_assertion(kb, entity.id, "Ada", asserted_at=T1, valid_from=T1)

        assert kb.as_of(T0).query("Person").where(name="Ada").all() == []
        assert len(kb.as_of(T1).query("Person").where(name="Ada").all()) == 1

    def test_query_where_respects_closed_validity_window(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=T0, valid_to=T1)

        # Visible just before T1
        assert len(kb.as_of(T1 - timedelta(seconds=1)).query("Person").where(name="Ada").all()) == 1
        # Not visible at T1 (half-open interval)
        assert kb.as_of(T1).query("Person").where(name="Ada").all() == []

    def test_query_count_at_different_times(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        advance_days = 180
        e1 = kb.create_entity("Person", author=AUTHOR)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=advance_days)
        e2 = kb.create_entity("Person", author=AUTHOR)
        e2_time = T0 + timedelta(days=advance_days)

        assert kb.as_of(T0).query("Person").count() == 1
        assert kb.as_of(e2_time).query("Person").count() == 2
        _ = e1, e2  # used for side effects


# ===========================================================================
# Property-based test — reconstruction invariant
# ===========================================================================


@st.composite
def assertion_scenarios(draw: st.DrawFn) -> tuple[list[dict[str, object]], int]:
    """Generate a list of assertions with varied temporal fields and a query time."""
    n = draw(st.integers(min_value=1, max_value=8))
    days_range = st.integers(min_value=0, max_value=400)

    records = []
    for _ in range(n):
        asserted_offset = draw(days_range)
        valid_from_offset = draw(st.one_of(st.none(), days_range))
        valid_to_offset = draw(st.one_of(st.none(), days_range))
        value = draw(st.text(min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("Lu", "Ll"))))
        records.append({
            "asserted_offset": asserted_offset,
            "valid_from_offset": valid_from_offset,
            "valid_to_offset": valid_to_offset,
            "value": value,
        })

    query_offset = draw(days_range)
    return records, query_offset


@given(scenario=assertion_scenarios())
@settings(max_examples=100, deadline=5000, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_as_of_reconstruction_matches_theoretical_filter(
    scenario: tuple[list[dict[str, object]], int],
) -> None:
    """Property: as_of(t) returns exactly the assertions whose temporal fields satisfy
    the bitemporal filter: valid_from <= t < (valid_to or ∞) AND asserted_at <= t.
    """
    records, query_offset = scenario
    query_t = T0 + timedelta(days=query_offset)

    with tempfile.TemporaryDirectory() as tmpdir:
        clock = FixedClock(T0)
        ids = FixedIdProvider([f"id-{i}" for i in range(len(records) + 10)])
        kb = Ontology.connect(Path(tmpdir) / "prop.db", clock=clock, id_provider=ids)
        kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
        entity = kb.create_entity("Person", author=AUTHOR)

        inserted: list[Assertion] = []
        for rec in records:
            asserted_at = T0 + timedelta(days=int(rec["asserted_offset"]))
            valid_from = (
                T0 + timedelta(days=int(rec["valid_from_offset"]))
                if rec["valid_from_offset"] is not None
                else None
            )
            valid_to = (
                T0 + timedelta(days=int(rec["valid_to_offset"]))
                if rec["valid_to_offset"] is not None
                else None
            )
            a = _put_assertion(
                kb, entity.id, str(rec["value"]),
                asserted_at=asserted_at,
                valid_from=valid_from,
                valid_to=valid_to,
            )
            inserted.append(a)

        # Theoretical filter
        INF = datetime(9999, 12, 31, tzinfo=UTC)

        def is_visible(a: Assertion) -> bool:
            vf = a.valid_from if a.valid_from is not None else datetime(1970, 1, 1, tzinfo=UTC)
            vt = a.valid_to if a.valid_to is not None else INF
            return vf <= query_t and query_t < vt and a.asserted_at <= query_t

        expected_ids = {a.id for a in inserted if is_visible(a)}
        actual_ids = {a.id for a in kb.as_of(query_t).assertions(subject=entity.id)}

        assert actual_ids == expected_ids
