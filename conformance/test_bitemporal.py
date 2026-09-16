"""Conformance vectors for SPEC §11.4 bitemporal as_of() time-travel.

Two time dimensions:
  valid_from / valid_to — when the fact was true in the real world
  asserted_at           — when we learned about the fact

An assertion is visible at time t iff:
  valid_from <= t < (valid_to or ∞)  AND  asserted_at <= t

This is the full rule for an assertion that has never changed status. Two
statuses add a further exclusion on top of it: `flagged` (reconstructed
point-in-time from the event log on every `as_of`-capable read path —
`assertions()` already did this; the `QueryBuilder`-facing trio
(`entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()`)
only since KI-097 fixed their current-status-based version of this exact
bug — `include_flagged` opts back in) and, for `retracted` specifically,
once its own retraction event's assertion-time has passed (ADR-0049,
KI-095; `include_history` opts back in on every `as_of`-capable read
path — `assertions()` only since KI-098 closed the mirror-image gap
ADR-0049 left it with: the `QueryBuilder`-facing trio got the
`include_history` opt-out from ADR-0049's own retraction-exclusion
fix, `assertions()` did not — see `TestAsOfRetractionAndFlagging`
below). `superseded` needs no such exclusion: its `valid_to` closure
already encodes the real-world end point, so the plain window check
above already handles it correctly.

All tests use injected clocks and IDs. Property tests verify reconstruction
against the theoretical filter using Hypothesis — scoped to assertions that
never change status, where the filter above is exact.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import Assertion, Clock, FixedClock, FixedIdProvider, IdProvider
from ontolith.schema import ConceptDef, PropertyDef, SchemaIR
from ontolith.store.duckdb import DuckDBBackend
from ontolith.store.sqlite import SQLiteBackend

T0 = datetime(2025, 1, 1, tzinfo=UTC)
T1 = datetime(2025, 6, 1, tzinfo=UTC)
T2 = datetime(2025, 12, 1, tzinfo=UTC)
T_BEFORE = datetime(2024, 12, 1, tzinfo=UTC)
AUTHOR = "alice@example.com"


def _kb(make_kb: KbFactory) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(30)])
    kb = make_kb(clock, ids)
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    return kb


def _connect(backend_name: str, path: Path, clock: Clock, id_provider: IdProvider) -> Ontology:
    """Build an Ontology directly for @given-decorated tests — see the
    identical helper in test_append_only_properties.py for why make_kb/
    tmp_path aren't used here (Hypothesis's function_scoped_fixture health
    check).
    """
    backend_cls = SQLiteBackend if backend_name == "sqlite" else DuckDBBackend
    return Ontology(backend_cls(path, clock=clock), clock=clock, id_provider=id_provider)


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

    def test_assertion_visible_at_asserted_time(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0)

        visible = kb.as_of(T0).assertions(subject=entity.id)
        assert len(visible) == 1
        assert visible[0].value == "Ada"

    def test_assertion_not_visible_before_asserted(self, make_kb: KbFactory) -> None:
        """asserted_at = T1; as_of(T0) must not reveal it."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T1)

        assert kb.as_of(T0).assertions(subject=entity.id) == []

    def test_assertion_excluded_when_validity_window_closed(self, make_kb: KbFactory) -> None:
        """valid_to = T1; as_of(T1) must not show it (half-open interval: t < valid_to)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=T0, valid_to=T1)

        assert kb.as_of(T1).assertions(subject=entity.id) == []

    def test_assertion_excluded_when_validity_not_yet_started(self, make_kb: KbFactory) -> None:
        """valid_from = T2; as_of(T1) must not show it."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=T2)

        assert kb.as_of(T1).assertions(subject=entity.id) == []

    def test_assertion_visible_just_before_valid_to(self, make_kb: KbFactory) -> None:
        """valid_to = T1; as_of(T1 - 1s) is still inside the window."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=T0, valid_to=T1)
        just_before = T1 - timedelta(seconds=1)

        visible = kb.as_of(just_before).assertions(subject=entity.id)
        assert len(visible) == 1

    def test_null_valid_from_treated_as_always_started(self, make_kb: KbFactory) -> None:
        """valid_from = NULL means open from the beginning; visible as long as asserted_at <= t."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=None)

        visible = kb.as_of(T0).assertions(subject=entity.id)
        assert len(visible) == 1

    def test_string_iso_accepted(self, make_kb: KbFactory) -> None:
        """as_of() must accept ISO-format strings as well as datetimes."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0)

        visible = kb.as_of(T0.isoformat()).assertions(subject=entity.id)
        assert len(visible) == 1

    def test_naive_datetime_treated_as_utc(self, make_kb: KbFactory) -> None:
        """A naive `t` (no tzinfo) must be normalized to UTC, not compared
        as a raw ISO string against UTC-aware stored timestamps — otherwise
        it silently misorders instead of matching T0's assertion."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0)

        naive_t0 = T0.replace(tzinfo=None)
        visible = kb.as_of(naive_t0).assertions(subject=entity.id)
        assert len(visible) == 1
        assert visible[0].value == "Ada"

    def test_naive_iso_string_treated_as_utc(self, make_kb: KbFactory) -> None:
        """Same as test_naive_datetime_treated_as_utc, for the string input path."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0)

        naive_iso = T0.replace(tzinfo=None).isoformat()
        visible = kb.as_of(naive_iso).assertions(subject=entity.id)
        assert len(visible) == 1

    def test_open_window_visible_well_into_future(self, make_kb: KbFactory) -> None:
        """An assertion with valid_to=NULL remains visible at any future t."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=T0)

        assert len(kb.as_of(T2).assertions(subject=entity.id)) == 1

    def test_no_status_filter_applied(self, make_kb: KbFactory) -> None:
        """as_of() does not filter non-flagged statuses — temporal dims decide
        visibility. ('flagged' is the one exception — see TestAsOfRetractionAndFlagging.)
        """
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        # Insert a 'superseded' assertion that was valid at T0
        _put_assertion(
            kb, entity.id, "Ada", asserted_at=T0, valid_from=T0, valid_to=T1, status="superseded"
        )

        visible = kb.as_of(T0).assertions(subject=entity.id)
        assert len(visible) == 1
        assert visible[0].status == "superseded"


# ===========================================================================
# Supersession chain time-travel
# ===========================================================================


class TestAsOfSupersession:
    """Time-travel across a supersession chain for time_varying properties."""

    def _setup_supersession(self, make_kb: KbFactory) -> tuple[Ontology, str]:
        """Returns kb and entity_id with two employments: Acme[T0,T1) → Beta[T1,∞)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        # Acme: [T0, T1) superseded
        _put_assertion(
            kb,
            entity.id,
            "Acme Corp",
            asserted_at=T0,
            valid_from=T0,
            valid_to=T1,
            status="superseded",
        )
        # Beta: [T1, ∞) active, asserted at T1
        _put_assertion(
            kb,
            entity.id,
            "Beta Inc",
            asserted_at=T1,
            valid_from=T1,
        )
        return kb, entity.id

    def test_superseded_assertion_visible_before_supersession(self, make_kb: KbFactory) -> None:
        kb, eid = self._setup_supersession(make_kb)
        visible = kb.as_of(T0).assertions(subject=eid, predicate="Person.name")
        assert len(visible) == 1
        assert visible[0].value == "Acme Corp"

    def test_new_assertion_not_visible_before_its_assertion_time(self, make_kb: KbFactory) -> None:
        kb, eid = self._setup_supersession(make_kb)
        # Beta was asserted at T1; as_of(T0) must not reveal it
        visible = kb.as_of(T0).assertions(subject=eid, predicate="Person.name")
        assert all(a.value != "Beta Inc" for a in visible)

    def test_as_of_after_supersession_shows_new_assertion(self, make_kb: KbFactory) -> None:
        kb, eid = self._setup_supersession(make_kb)
        visible = kb.as_of(T2).assertions(subject=eid, predicate="Person.name")
        assert len(visible) == 1
        assert visible[0].value == "Beta Inc"

    def test_at_transition_time_only_new_assertion_visible(self, make_kb: KbFactory) -> None:
        """At exactly T1: Acme window is [T0, T1) so T1 is excluded; Beta starts at T1."""
        kb, eid = self._setup_supersession(make_kb)
        visible = kb.as_of(T1).assertions(subject=eid, predicate="Person.name")
        assert len(visible) == 1
        assert visible[0].value == "Beta Inc"


# ===========================================================================
# Retraction closes valid_to; flagged excluded by default
# ===========================================================================


class TestAsOfRetractionAndFlagging:
    """Retraction closes an *open-ended* valid_to (SPEC §5) — but, per
    ADR-0049 (KI-095), not one already set explicitly to a later date; the
    `test_retract_with_explicit_future_valid_to_*` vectors below exist
    precisely because that case relies on a second, independent mechanism
    (the retraction event's own assertion-time), not the window closure
    this docstring used to claim covers every case. Flagged assertions are
    excluded from as_of by default, same as default (non-as_of) queries
    (SPEC §10.3).
    """

    def test_retract_visible_as_of_before_retraction(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        before_retraction = clock.now()
        clock.advance(days=1)
        kb.retract(active[0].id, AUTHOR)

        visible = kb.as_of(before_retraction).assertions(subject=entity.id)
        assert len(visible) == 1
        assert visible[0].id == active[0].id

    def test_retract_not_visible_as_of_after_retraction(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        kb.retract(active[0].id, AUTHOR)
        after_retraction = clock.now()
        clock.advance(days=1)

        visible = kb.as_of(after_retraction).assertions(subject=entity.id, predicate="Person.name")
        assert visible == []

    def test_retract_with_explicit_future_valid_to_not_visible_once_known(
        self, make_kb: KbFactory
    ) -> None:
        """ADR-0049 (KI-095): unlike the test above, this assertion has an
        explicit, far-future valid_to — the case _retraction_valid_to()
        deliberately leaves un-narrowed, so the window alone can't tell
        this apart from a still-active fact. assertions() must additionally
        exclude it once its own retraction event's timestamp is known."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", AUTHOR, valid_to=T2 + timedelta(days=3650)
        )

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        kb.retract(a.id, AUTHOR)
        after_retraction = clock.now()

        visible = kb.as_of(after_retraction).assertions(subject=entity.id, predicate="Person.name")
        assert visible == []

    def test_retract_with_explicit_future_valid_to_still_visible_before_retraction(
        self, make_kb: KbFactory
    ) -> None:
        """The exclusion is assertion-time-scoped, not blanket: a t before
        the retraction event's own timestamp must still show the value."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", AUTHOR, valid_to=T2 + timedelta(days=3650)
        )
        before_retraction = kb.clock.now()

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        kb.retract(a.id, AUTHOR)

        visible = kb.as_of(before_retraction).assertions(subject=entity.id, predicate="Person.name")
        assert len(visible) == 1
        assert visible[0].id == a.id

    def test_include_history_opts_back_into_a_retracted_assertion_via_assertions(
        self, make_kb: KbFactory
    ) -> None:
        """KI-098: assertions() gains the same include_history opt-out the
        QueryBuilder-facing trio already had (ADR-0049) — the asymmetry
        where kb.as_of(t).query(Concept).include_history() could surface an
        entity but kb.as_of(t).assertions(...) could not surface the
        assertion behind it."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", AUTHOR, valid_to=T2 + timedelta(days=3650)
        )

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        kb.retract(a.id, AUTHOR)
        after_retraction = clock.now()

        default = kb.as_of(after_retraction).assertions(subject=entity.id, predicate="Person.name")
        assert default == []

        visible = kb.as_of(after_retraction).assertions(
            subject=entity.id, predicate="Person.name", include_history=True
        )
        assert len(visible) == 1
        assert visible[0].id == a.id

    def test_flagged_excluded_from_as_of_by_default(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)  # -> contradiction

        now = kb.clock.now()
        visible = kb.as_of(now).assertions(subject=entity.id, predicate="Person.name")
        assert visible == []

    def test_flagged_included_with_include_flagged_true(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)

        now = kb.clock.now()
        visible = kb.as_of(now).assertions(
            subject=entity.id, predicate="Person.name", include_flagged=True
        )
        assert len(visible) == 2
        assert {a.value for a in visible} == {"Ada", "Ava"}

    def test_as_of_before_dispute_shows_predispute_value(self, make_kb: KbFactory) -> None:
        """as_of(t) for t strictly before a contradiction arose must still
        show the value that was active and undisputed at t - flagging is
        permanent (never sets valid_to), so using current status instead of
        status-at-t would erase history from before the flag existed."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        before_dispute = kb.clock.now()

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)  # -> contradiction

        visible = kb.as_of(before_dispute).assertions(subject=entity.id, predicate="Person.name")
        assert len(visible) == 1
        assert visible[0].value == "Ada"

    def test_as_of_well_into_an_open_dispute_still_excludes_both_members(
        self, make_kb: KbFactory
    ) -> None:
        """as_of(t) for t well after a contradiction opened (and still
        unresolved) excludes both members - the fix must correctly exclude
        at any t within the disputed window, not just t == now."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)  # -> contradiction
        clock.advance(days=5)  # still unresolved, well after the dispute started

        visible = kb.as_of(clock.now()).assertions(subject=entity.id, predicate="Person.name")
        assert visible == []

    def test_as_of_mid_dispute_excludes_even_after_later_resolution(
        self, make_kb: KbFactory
    ) -> None:
        """as_of(t) for t between when a contradiction opened and when it
        was later resolved must still show nothing active - resolution must
        not retroactively "unflag" the disputed window that already passed."""
        kb = _kb(make_kb)
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)
        mid_dispute = clock.now()
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        clock.advance(days=1)
        winner = flagged[0]
        kb.resolve_contradiction(contradiction.id, winner.id, "carol@example.com")

        visible = kb.as_of(mid_dispute).assertions(subject=entity.id, predicate="Person.name")
        assert visible == []

    def test_query_as_of_before_dispute_shows_predispute_value(self, make_kb: KbFactory) -> None:
        """KI-097: `.query()` (`entities_where()`) counterpart to
        `test_as_of_before_dispute_shows_predispute_value` above — before
        this fix, `entities_where()`'s `as_of` branch excluded `flagged` by
        *current* status, not point-in-time, so this case (unlike
        `assertions()`'s, which already reconstructed correctly) wrongly
        excluded the predispute value too."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        before_dispute = kb.clock.now()

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)  # -> contradiction

        results = kb.as_of(before_dispute).query("Person").where(name="Ada").all()
        assert {r.id for r in results} == {entity.id}

    def test_query_as_of_mid_dispute_excludes_even_after_later_resolution(
        self, make_kb: KbFactory
    ) -> None:
        """KI-097: `.query()` counterpart to
        `test_as_of_mid_dispute_excludes_even_after_later_resolution`
        above — the exact shape of this KI's bug: before the fix, a `t`
        during a since-resolved dispute wrongly matched, because current
        status (now `active` again, post-resolution) was checked instead
        of status-at-t."""
        kb = _kb(make_kb)
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author=AUTHOR)
        first = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)
        mid_dispute = clock.now()
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        clock.advance(days=1)
        kb.resolve_contradiction(contradiction.id, first.id, "carol@example.com")

        assert kb.as_of(mid_dispute).query("Person").where(name="Ada").all() == []

    def test_as_of_after_same_instant_flag_and_resolve_shows_winner(
        self, make_kb: KbFactory
    ) -> None:
        """Regression: if a contradiction is flagged and resolved without
        the clock advancing between them (both events share the same `at`),
        the winner's 'reactivated' event must still win the tiebreak in
        as_of()'s event-log reconstruction - not silently hidden because
        `at` alone can't order two events recorded at the same instant."""
        kb = _kb(make_kb)
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)  # -> contradiction

        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None
        winner = flagged[0]

        # No clock.advance() here - resolution happens at the same instant
        # the flag did.
        kb.resolve_contradiction(contradiction.id, winner.id, "carol@example.com")

        visible = kb.as_of(kb.clock.now()).assertions(subject=entity.id, predicate="Person.name")
        assert len(visible) == 1
        assert visible[0].id == winner.id

    def test_query_as_of_after_same_instant_flag_and_resolve_shows_winner(
        self, make_kb: KbFactory
    ) -> None:
        """KI-097: `.query()` counterpart to the `assertions()` regression
        above, on all three `QueryBuilder`-facing surfaces that got the
        ported reconstruction (`.where()`, `.min_confidence()`,
        `.trust_at_least()`) — each has its own copy of the `ae.id DESC`
        tiebreak, so each needs its own same-instant-flag-and-resolve
        pin; a mutation dropping the tiebreak on any one of the three
        survives the other two."""
        kb = _kb(make_kb)
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR, confidence=0.9)
        kb.assert_literal(
            entity.id, "Person.name", "Ava", "Text", AUTHOR, confidence=0.9
        )  # -> contradiction

        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None
        winner = flagged[0]

        # No clock.advance() here - resolution happens at the same instant
        # the flag did.
        kb.resolve_contradiction(contradiction.id, winner.id, "carol@example.com")

        t = kb.clock.now()
        where_results = kb.as_of(t).query("Person").where(name=winner.value).all()
        assert {r.id for r in where_results} == {entity.id}
        confidence_results = kb.as_of(t).query("Person").min_confidence(0.5).all()
        assert {r.id for r in confidence_results} == {entity.id}
        trust_results = kb.as_of(t).query("Person").trust_at_least(0).all()
        assert {r.id for r in trust_results} == {entity.id}

    def test_contradiction_resolution_closes_losers_valid_to(self, make_kb: KbFactory) -> None:
        """A retracted (losing) contradiction member's window closes at
        resolution time; as_of() after resolution must not show it."""
        kb = _kb(make_kb)
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        winner = flagged[0]
        kb.resolve_contradiction(contradiction.id, winner.id, "carol@example.com")
        after_resolution = clock.now()

        visible = kb.as_of(after_resolution).assertions(
            subject=entity.id, predicate="Person.name", include_flagged=True
        )
        assert {a.id for a in visible} == {winner.id}

    def test_retract_never_widens_an_already_closed_valid_to(self, make_kb: KbFactory) -> None:
        """Retracting an already-superseded assertion must not push its
        valid_to forward — that would resurrect it as visible in as_of
        queries between the original close point and the retraction time,
        corrupting history (bitemporal.md invariants 1-2)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        # Acme: [T0, T1) already closed by supersession
        acme = _put_assertion(
            kb,
            entity.id,
            "Acme Corp",
            asserted_at=T0,
            valid_from=T0,
            valid_to=T1,
            status="superseded",
        )
        _put_assertion(kb, entity.id, "Beta Inc", asserted_at=T1, valid_from=T1)

        # Retract the already-closed Acme record long after it was superseded
        kb.retract(acme.id, AUTHOR)

        retracted = [
            a
            for a in kb.backend.assertions(subject=entity.id, predicate="Person.name", status=None)
            if a.id == acme.id
        ]
        assert retracted[0].status == "retracted"
        assert retracted[0].valid_to == T1  # unchanged, not widened to clock.now()

        # A query strictly between the supersession and the retraction must
        # NOT show Acme as concurrently active with Beta.
        between = kb.as_of(T1 + timedelta(days=1)).assertions(
            subject=entity.id, predicate="Person.name"
        )
        assert {a.value for a in between} == {"Beta Inc"}

    def test_retract_still_closes_an_open_valid_to(self, make_kb: KbFactory) -> None:
        """Sanity check: the fix for the widening bug must not regress the
        ordinary case of retracting a still-open assertion."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        retraction_time = clock.now()
        kb.retract(active[0].id, AUTHOR)

        retracted = kb.backend.assertions(
            subject=entity.id, predicate="Person.name", status="retracted"
        )
        assert retracted[0].valid_to == retraction_time


# ===========================================================================
# Entity-level time-travel via as_of().query()
# ===========================================================================


class TestAsOfQuery:
    """kb.as_of(t).query(concept) reconstructs entity set at time t."""

    def test_entity_not_visible_before_creation(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        # Entity created at T0 (default clock)
        kb.create_entity("Person", author=AUTHOR)

        assert kb.as_of(T_BEFORE).query("Person").all() == []

    def test_entity_visible_at_creation_time(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        kb.create_entity("Person", author=AUTHOR)

        assert len(kb.as_of(T0).query("Person").all()) == 1

    def test_query_where_applies_temporal_filter(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        # Assertion recorded at T1, so invisible as_of T0
        _put_assertion(kb, entity.id, "Ada", asserted_at=T1, valid_from=T1)

        assert kb.as_of(T0).query("Person").where(name="Ada").all() == []
        assert len(kb.as_of(T1).query("Person").where(name="Ada").all()) == 1

    def test_query_where_respects_closed_validity_window(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        _put_assertion(kb, entity.id, "Ada", asserted_at=T0, valid_from=T0, valid_to=T1)

        # Visible just before T1
        assert len(kb.as_of(T1 - timedelta(seconds=1)).query("Person").where(name="Ada").all()) == 1
        # Not visible at T1 (half-open interval)
        assert kb.as_of(T1).query("Person").where(name="Ada").all() == []

    def test_query_where_relation_filter_applies_temporal_filter(self, make_kb: KbFactory) -> None:
        """A relation-target-id filter (value_ref) respects as_of, same as a literal filter (KI-030)."""
        kb = _kb(make_kb)
        person = kb.create_entity("Person", author=AUTHOR)
        org = kb.create_entity("Organization", author=AUTHOR)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=(T1 - T0).days)
        kb.assert_ref(person.id, "Person.employer", org.id, AUTHOR)

        assert kb.as_of(T0).query("Person").where(employer=org.id).all() == []
        assert len(kb.as_of(T1).query("Person").where(employer=org.id).all()) == 1

    def test_query_count_at_different_times(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
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
# Schema resolution at a point in time (KI-019)
# ===========================================================================


class TestAsOfSchema:
    """kb.as_of(t).schema() resolves the schema version effective at t, not
    always the latest — required for as_of() to correctly interpret a
    property's temporality/cardinality as of a point in time before a
    later schema migration changed them (SPEC §11.4: "Schema is resolved
    to the schema_version effective at t"; SPEC §19)."""

    ADMIN = "admin@example.com"

    def _admin_kb(self, make_kb: KbFactory) -> Ontology:
        kb = _kb(make_kb)
        kb.create_principal(self.ADMIN, kind="human", default_capability="admin")
        return kb

    def test_schema_none_before_any_version_applied(self, make_kb: KbFactory) -> None:
        kb = self._admin_kb(make_kb)
        assert kb.as_of(T0).schema() is None

    def test_schema_none_before_first_version_effective(self, make_kb: KbFactory) -> None:
        kb = self._admin_kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)  # schema applied at T0 + 1 day
        kb.apply_schema(SchemaIR(namespace="default", version=1), self.ADMIN)

        assert kb.as_of(T0).schema() is None

    def test_schema_resolves_version_effective_at_t(self, make_kb: KbFactory) -> None:
        kb = self._admin_kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)

        kb.apply_schema(SchemaIR(namespace="default", version=1), self.ADMIN)
        clock.set(T1)
        kb.apply_schema(SchemaIR(namespace="default", version=2), self.ADMIN)

        assert kb.as_of(T0).schema().version == 1  # type: ignore[union-attr]
        assert kb.as_of(T1).schema().version == 2  # type: ignore[union-attr]

    def test_schema_effective_exactly_at_applied_at_is_inclusive(self, make_kb: KbFactory) -> None:
        """Boundary matches the rest of the bitemporal model's t <= applied_at
        convention (e.g. valid_from <= t) rather than a strict '<'."""
        kb = self._admin_kb(make_kb)
        kb.apply_schema(SchemaIR(namespace="default", version=1), self.ADMIN)

        assert kb.as_of(T0).schema().version == 1  # type: ignore[union-attr]

    def test_schema_migration_changes_reconstructed_temporality(self, make_kb: KbFactory) -> None:
        """SPEC §19's own suggested vector: a property's temporality as
        reconstructed by as_of() must reflect what the schema said at that
        point in time, not what it says today."""
        kb = self._admin_kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)

        v1 = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "employer": PropertyDef(
                            name="employer", value_type="Text", temporality="static"
                        ),
                    },
                ),
            },
        )
        kb.apply_schema(v1, self.ADMIN)
        clock.set(T1)  # migrate employer to time_varying

        v2 = SchemaIR(
            namespace="default",
            version=2,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "employer": PropertyDef(
                            name="employer", value_type="Text", temporality="time_varying"
                        ),
                    },
                ),
            },
        )
        kb.apply_schema(v2, self.ADMIN)

        pre_migration = kb.as_of(T0).schema()
        post_migration = kb.as_of(T1).schema()
        assert pre_migration is not None
        assert post_migration is not None
        assert pre_migration.temporality_of("Person.employer") == "static"
        assert post_migration.temporality_of("Person.employer") == "time_varying"

        # get_schema() (no time argument) always reflects latest, unaffected
        # by which point in time as_of() is reconstructing.
        assert kb.backend.get_schema("default").version == 2  # type: ignore[union-attr]


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
        value = draw(
            st.text(
                min_size=1, max_size=10, alphabet=st.characters(whitelist_categories=("Lu", "Ll"))
            )
        )
        records.append(
            {
                "asserted_offset": asserted_offset,
                "valid_from_offset": valid_from_offset,
                "valid_to_offset": valid_to_offset,
                "value": value,
            }
        )

    query_offset = draw(days_range)
    return records, query_offset


@given(scenario=assertion_scenarios())
@settings(
    max_examples=100, deadline=5000, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_as_of_reconstruction_matches_theoretical_filter(
    scenario: tuple[list[dict[str, object]], int],
    backend_name: str,
) -> None:
    """Property: as_of(t) returns exactly the assertions whose temporal fields satisfy
    the bitemporal filter: valid_from <= t < (valid_to or ∞) AND asserted_at <= t.
    """
    records, query_offset = scenario
    query_t = T0 + timedelta(days=query_offset)

    with tempfile.TemporaryDirectory() as tmpdir:
        clock = FixedClock(T0)
        ids = FixedIdProvider([f"id-{i}" for i in range(len(records) + 10)])
        kb = _connect(backend_name, Path(tmpdir) / "prop.db", clock, ids)
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
                kb,
                entity.id,
                str(rec["value"]),
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
        kb.close()  # release SQLite file lock before TemporaryDirectory cleanup (Windows)


@given(
    n_asserts=st.integers(min_value=1, max_value=5),
    retract_after=st.lists(st.booleans(), min_size=1, max_size=5),
    query_offset=st.integers(min_value=0, max_value=15),
)
@settings(
    max_examples=50,
    deadline=None,  # each example runs real governed propose/retract cycles
    # (conflict routing + policy evaluation), not raw inserts like the
    # sibling property test above — Windows CI SQLite I/O is measurably
    # slower, so a fixed deadline flakes; correctness is what matters here.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_as_of_visibility_matches_ground_truth_across_assert_retract_sequence(
    n_asserts: int,
    retract_after: list[bool],
    query_offset: int,
    backend_name: str,
) -> None:
    """Property: for any sequence of static-predicate assertions (distinct
    values, so every 2nd+ triggers a contradiction) with optional retraction
    after each, as_of(t) visibility exactly matches the bitemporal formula
    applied to the assertions' *actual persisted* temporal fields, plus
    flagged-status *at query_t* (not current status — a static conflict
    flags an assertion permanently, so current status can't tell you
    whether it was flagged yet at some earlier t). Ground truth for the
    temporal fields is the non-as_of status=None query; ground truth for
    flagged-at-t is reconstructed from the assertion_event log, mirroring
    exactly what the as_of() SQL under test does.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        clock = FixedClock(T0)
        ids = FixedIdProvider([f"id-{i}" for i in range(n_asserts * 3 + 10)])
        kb = _connect(backend_name, Path(tmpdir) / "prop2.db", clock, ids)
        kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
        entity = kb.create_entity("Person", author=AUTHOR)

        for i in range(n_asserts):
            a = kb.assert_literal(entity.id, "Person.name", f"value-{i}", "Text", AUTHOR)
            clock.advance(days=1)
            if i < len(retract_after) and retract_after[i] and a.status == "active":
                kb.retract(a.id, AUTHOR)
                clock.advance(days=1)

        query_t = T0 + timedelta(days=query_offset)
        ground_truth = kb.backend.assertions(
            subject=entity.id, predicate="Person.name", status=None
        )

        INF = datetime(9999, 12, 31, tzinfo=UTC)

        def flagged_at(a: Assertion, t: datetime) -> bool:
            events = [
                e
                for e in kb.backend.get_assertion_events(a.id)
                if e.at <= t and e.action in ("flagged", "reactivated")
            ]
            if not events:
                return False
            return max(events, key=lambda e: e.at).action == "flagged"

        def is_visible(a: Assertion, include_flagged: bool) -> bool:
            vf = a.valid_from if a.valid_from is not None else datetime(1970, 1, 1, tzinfo=UTC)
            vt = a.valid_to if a.valid_to is not None else INF
            temporally_visible = vf <= query_t < vt and a.asserted_at <= query_t
            return temporally_visible and (include_flagged or not flagged_at(a, query_t))

        for include_flagged in (False, True):
            expected_ids = {a.id for a in ground_truth if is_visible(a, include_flagged)}
            actual_ids = {
                a.id
                for a in kb.as_of(query_t).assertions(
                    subject=entity.id, predicate="Person.name", include_flagged=include_flagged
                )
            }
            assert actual_ids == expected_ids

        kb.close()


@given(
    n_values=st.integers(min_value=1, max_value=5),
    retract_indices=st.lists(st.integers(min_value=0, max_value=4), max_size=5),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_valid_to_never_widens_once_closed(
    n_values: int, retract_indices: list[int], backend_name: str
) -> None:
    """Property: once an assertion's valid_to is closed (by supersession or
    retraction), no later operation - including retracting that same
    already-closed record - may change it to a different value. Regression
    for the bug where retract() unconditionally overwrote valid_to even when
    already set, silently reopening history."""
    with tempfile.TemporaryDirectory() as tmpdir:
        clock = FixedClock(T0)
        ids = FixedIdProvider([f"id-{i}" for i in range(n_values * 3 + 10)])
        kb = _connect(backend_name, Path(tmpdir) / "prop3.db", clock, ids)
        kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(
                            name="name", value_type="Text", temporality="time_varying"
                        ),
                    },
                ),
            },
        )
        kb.backend.put_schema(schema)
        entity = kb.create_entity("Person", author=AUTHOR)

        all_ids: list[str] = []
        for i in range(n_values):
            a = kb.assert_literal(entity.id, "Person.name", f"value-{i}", "Text", AUTHOR)
            all_ids.append(a.id)
            clock.advance(days=1)

        # Snapshot valid_to right after the chain is built (before any retracts).
        before_retract = {
            a.id: a.valid_to
            for a in kb.backend.assertions(subject=entity.id, predicate="Person.name", status=None)
        }

        for idx in retract_indices:
            if idx < len(all_ids):
                kb.retract(all_ids[idx], AUTHOR)
                clock.advance(days=1)

        after = {
            a.id: a.valid_to
            for a in kb.backend.assertions(subject=entity.id, predicate="Person.name", status=None)
        }

        for aid, vt_before in before_retract.items():
            if vt_before is not None:
                assert after[aid] == vt_before, (
                    f"assertion {aid} had valid_to={vt_before} before retraction "
                    f"attempts but was changed to {after[aid]}"
                )

        kb.close()
