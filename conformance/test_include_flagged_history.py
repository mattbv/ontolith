"""Conformance vectors for `QueryBuilder.include_flagged()` /
`include_history()` (KI-081, SPEC §11.2 / §10.3).

By default a `.where()` filter matches only `active` assertions, so a
flagged (contradicted), superseded, or retracted value is invisible to
`kb.query(Concept).where(...)`. `.include_flagged()` widens the match set
to also include `flagged`; `.include_history()` also includes `superseded`
and `retracted`. Both are match-set wideners — `.all()` still returns
`list[Entity]`. The two are independent (one does not imply the other).

`TestComposesWithConfidenceAndTrust` covers the KI-093 follow-up: the same
widening must also apply to `.min_confidence()`/`.trust_at_least()`
(`entities_meeting_confidence`/`entities_meeting_trust`), not just
`.where()` — otherwise a no-op floor silently re-narrows the result back
to `active` only.

`tests/unit/test_query.py` only exercises SQLite (`Ontology.connect()`
always builds a `SQLiteBackend`); this file pins the same behavior on
DuckDB too, since the widening lives in each backend's own
`entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.schema import ConceptDef, PropertyDef, SchemaIR

T0 = datetime(2025, 1, 1, tzinfo=UTC)

ADMIN = "admin@example.com"
AUTHOR = "author@example.com"


def _kb(make_kb: KbFactory) -> Ontology:
    """Schema with a static `Person.name` and a time_varying `Person.role`."""
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(40)])
    kb = make_kb(clock, ids)
    kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    kb.apply_schema(
        SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text"),
                        "role": PropertyDef(
                            name="role", value_type="Text", temporality="time_varying"
                        ),
                    },
                ),
            },
        ),
        author=ADMIN,
    )
    return kb


class TestIncludeFlagged:
    def test_flagged_value_excluded_by_default_included_on_opt_in(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        person = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(person.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.assert_literal(person.id, "Person.name", "Ava", "Text", AUTHOR)  # contradiction
        assert kb.assertions(subject=person.id, predicate="Person.name", status="flagged")

        assert kb.query("Person").where(name="Ada").all() == []
        assert {r.id for r in kb.query("Person").where(name="Ada").include_flagged().all()} == {
            person.id
        }

    def test_include_flagged_does_not_pull_in_superseded(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        person = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(person.id, "Person.role", "Engineer", "Text", AUTHOR)
        kb.assert_literal(person.id, "Person.role", "Manager", "Text", AUTHOR)  # supersedes
        assert kb.assertions(subject=person.id, predicate="Person.role", status="superseded")

        assert kb.query("Person").where(role="Engineer").include_flagged().all() == []


class TestIncludeHistory:
    def test_superseded_value_excluded_by_default_included_on_opt_in(
        self, make_kb: KbFactory
    ) -> None:
        kb = _kb(make_kb)
        person = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(person.id, "Person.role", "Engineer", "Text", AUTHOR)
        kb.assert_literal(person.id, "Person.role", "Manager", "Text", AUTHOR)
        assert kb.assertions(subject=person.id, predicate="Person.role", status="superseded")

        assert kb.query("Person").where(role="Engineer").all() == []
        assert {
            r.id for r in kb.query("Person").where(role="Engineer").include_history().all()
        } == {person.id}

    def test_retracted_value_included_on_opt_in(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        person = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(person.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.retract(a.id, AUTHOR)
        assert kb.assertions(subject=person.id, predicate="Person.name", status="retracted")

        assert kb.query("Person").where(name="Ada").all() == []
        assert {r.id for r in kb.query("Person").where(name="Ada").include_history().all()} == {
            person.id
        }

    def test_include_history_does_not_pull_in_flagged(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        person = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(person.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.assert_literal(person.id, "Person.name", "Ava", "Text", AUTHOR)
        assert kb.assertions(subject=person.id, predicate="Person.name", status="flagged")

        assert kb.query("Person").where(name="Ada").include_history().all() == []


class TestAsOfRetraction:
    """ADR-0049 (KI-095): `.as_of(t)` additionally excludes a `retracted`
    assertion once its own retraction event's assertion-time has passed,
    on top of the existing valid-time window check — a retracted
    assertion's window is not reliably narrowed at retraction time (an
    explicit, later `valid_to` is left untouched), so without this it kept
    matching `.as_of(t)` for any `t` its stale window still covered.
    `.include_history()` opts back out, the first thing it has ever done
    on the `as_of` path (previously a documented no-op there)."""

    def test_retracted_value_excluded_once_retraction_known(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        person = kb.create_entity("Person", author=AUTHOR)
        # An explicit, far-future valid_to is the case that actually
        # exposes the bug — _retraction_valid_to() only narrows an
        # open-ended window, never one already set explicitly.
        a = kb.assert_literal(
            person.id,
            "Person.name",
            "Ada",
            "Text",
            AUTHOR,
            valid_to=T0 + timedelta(days=3650),
        )
        clock.advance(days=10)
        kb.retract(a.id, AUTHOR)

        assert kb.as_of(T0 + timedelta(days=365)).query("Person").where(name="Ada").all() == []

    def test_retracted_value_still_visible_before_its_own_retraction_time(
        self, make_kb: KbFactory
    ) -> None:
        """The exclusion is assertion-time-scoped, not blanket: a `t`
        before the retraction ever happened must still see the value —
        we didn't know to exclude it yet, as of that `t`."""
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        person = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(
            person.id,
            "Person.name",
            "Ada",
            "Text",
            AUTHOR,
            valid_to=T0 + timedelta(days=3650),
        )
        before_retraction = clock.now()

        clock.advance(days=10)
        kb.retract(a.id, AUTHOR)

        results = kb.as_of(before_retraction).query("Person").where(name="Ada").all()
        assert {r.id for r in results} == {person.id}

    def test_include_history_opts_back_into_a_retracted_value_under_as_of(
        self, make_kb: KbFactory
    ) -> None:
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        person = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(
            person.id,
            "Person.name",
            "Ada",
            "Text",
            AUTHOR,
            valid_to=T0 + timedelta(days=3650),
        )
        clock.advance(days=10)
        kb.retract(a.id, AUTHOR)

        t = T0 + timedelta(days=365)
        assert kb.as_of(t).query("Person").where(name="Ada").all() == []
        results = kb.as_of(t).query("Person").where(name="Ada").include_history().all()
        assert {r.id for r in results} == {person.id}

    def test_include_flagged_and_include_history_combine_under_as_of_retraction(
        self, make_kb: KbFactory
    ) -> None:
        """The two opt-ins are independent (KI-081): combining
        .include_flagged() with .include_history() must still opt into a
        retracted value under .as_of(), not silently drop the retraction
        opt-out because a different flag was also set."""
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        person = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(
            person.id,
            "Person.name",
            "Ada",
            "Text",
            AUTHOR,
            valid_to=T0 + timedelta(days=3650),
        )
        clock.advance(days=10)
        kb.retract(a.id, AUTHOR)

        t = T0 + timedelta(days=365)
        results = (
            kb.as_of(t).query("Person").where(name="Ada").include_flagged().include_history().all()
        )
        assert {r.id for r in results} == {person.id}

    def test_excludes_exactly_at_the_retraction_instant(self, make_kb: KbFactory) -> None:
        """ADR-0049 states an inclusive-exclusive boundary: as_of(t) for t
        exactly equal to the retraction event's own assertion-time must
        already exclude — mirroring how asserted_at <= t makes an
        assertion visible starting exactly at its own asserted_at, not
        strictly after."""
        kb = _kb(make_kb)
        person = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(
            person.id,
            "Person.name",
            "Ada",
            "Text",
            AUTHOR,
            valid_to=T0 + timedelta(days=3650),
        )
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=10)
        kb.retract(a.id, AUTHOR)
        retraction_instant = clock.now()

        assert kb.as_of(retraction_instant).query("Person").where(name="Ada").all() == []


class TestBothFlags:
    def test_both_together_match_every_status(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        flagged = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(flagged.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.assert_literal(flagged.id, "Person.name", "Ava", "Text", AUTHOR)
        superseded = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(superseded.id, "Person.role", "Engineer", "Text", AUTHOR)
        kb.assert_literal(superseded.id, "Person.role", "Manager", "Text", AUTHOR)

        q = kb.query("Person").include_flagged().include_history()
        assert {r.id for r in q.where(name="Ada").all()} == {flagged.id}
        assert {
            r.id
            for r in kb.query("Person")
            .where(role="Engineer")
            .include_flagged()
            .include_history()
            .all()
        } == {superseded.id}


class TestComposesWithConfidenceAndTrust:
    """KI-093: `.include_flagged()`/`.include_history()` widen
    `.min_confidence()`/`.trust_at_least()` the same way they widen
    `.where()` — a no-op floor (`min_confidence(0.0)`, `trust_at_least(0)`)
    must not silently re-narrow an `.include_history()`/`.include_flagged()`
    result back to `active` only."""

    def test_include_history_min_confidence_zero_is_a_true_noop(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        person = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(person.id, "Person.name", "Ada", "Text", AUTHOR, confidence=0.9)
        kb.retract(a.id, AUTHOR)
        assert kb.assertions(subject=person.id, predicate="Person.name", status="retracted")

        without_floor = {r.id for r in kb.query("Person").where(name="Ada").include_history().all()}
        with_noop_floor = {
            r.id
            for r in kb.query("Person")
            .where(name="Ada")
            .include_history()
            .min_confidence(0.0)
            .all()
        }
        assert without_floor == {person.id}
        assert with_noop_floor == {person.id}

    def test_include_history_trust_at_least_zero_is_a_true_noop(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        person = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(person.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.retract(a.id, AUTHOR)

        with_noop_floor = {
            r.id
            for r in kb.query("Person").where(name="Ada").include_history().trust_at_least(0).all()
        }
        assert with_noop_floor == {person.id}

    def test_include_flagged_min_confidence_still_filters_below_threshold(
        self, make_kb: KbFactory
    ) -> None:
        """The widening isn't a bypass: a genuinely-failing confidence floor
        still excludes, even under `.include_flagged()`."""
        kb = _kb(make_kb)
        person = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(person.id, "Person.name", "Ada", "Text", AUTHOR, confidence=0.2)
        kb.assert_literal(person.id, "Person.name", "Ava", "Text", AUTHOR, confidence=0.2)
        assert kb.assertions(subject=person.id, predicate="Person.name", status="flagged")

        result = kb.query("Person").where(name="Ada").include_flagged().min_confidence(0.9).all()
        assert result == []
