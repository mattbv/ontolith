"""Conformance vectors for QueryBuilder.min_confidence()/.trust_at_least() (KI-028).

Both filters are pushed down to StorageBackend.entities_meeting_confidence()/
entities_meeting_trust() as a single (namespace, concept)-scoped lookup per
filter rather than a per-entity assertions()/get_principal() round trip —
this file proves both backends implement that push-down identically, since
tests/unit/test_query.py only ever exercises the SQLite backend directly.

TestAsOfConfidenceTrust additionally covers KI-036: both filters must thread
`.as_of()`'s pinned time through to that push-down instead of always
checking current-active state regardless of it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.schema import ConceptDef, PropertyDef, SchemaIR

T0 = datetime(2025, 1, 1, tzinfo=UTC)

TRUSTED = "trusted@example.com"
UNTRUSTED = "untrusted@example.com"
ADMIN = "admin@example.com"


def _kb(make_kb: KbFactory) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(20)])
    kb = make_kb(clock, ids)
    kb.create_principal(
        TRUSTED, kind="human", auth_method="oidc", default_capability="write", trust_level=8
    )
    kb.create_principal(
        UNTRUSTED, kind="human", auth_method="oidc", default_capability="write", trust_level=1
    )
    return kb


def _kb_time_varying(make_kb: KbFactory) -> Ontology:
    """`_kb()` plus a schema declaring `Person.employer` time_varying, for
    tests that need a real supersession boundary (SPEC §10.2) rather than a
    retraction - `Person.name` stays static (the default) via this schema."""
    kb = _kb(make_kb)
    kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
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
                },
            ),
        },
    )
    kb.apply_schema(schema, author=ADMIN)
    return kb


class TestMinConfidence:
    def test_excludes_none_and_below_threshold(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        high = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(high.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.9)

        low = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(low.id, "Person.name", "Grace", "Text", TRUSTED, confidence=0.3)

        no_confidence = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(no_confidence.id, "Person.name", "Bob", "Text", TRUSTED)

        results = kb.query("Person").min_confidence(0.5).all()
        assert {r.id for r in results} == {high.id}

    def test_no_qualifying_entities_returns_empty(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.2)

        assert kb.query("Person").min_confidence(0.9).all() == []

    def test_empty_concept_returns_empty(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        assert kb.query("Person").min_confidence(0.5).all() == []

    def test_none_confidence_excluded_even_at_threshold_zero(self, make_kb: KbFactory) -> None:
        """0.0 is a valid, satisfiable threshold - None must still never
        qualify (ADR-0004), not merely at higher thresholds where a
        `confidence >= threshold` vs. a buggy `COALESCE(confidence, 0) >=
        threshold` would be indistinguishable."""
        kb = _kb(make_kb)
        no_confidence = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(no_confidence.id, "Person.name", "Bob", "Text", TRUSTED)

        assert kb.query("Person").min_confidence(0.0).all() == []

    def test_boundary_threshold_is_inclusive(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        exact = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(exact.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.5)

        results = kb.query("Person").min_confidence(0.5).all()
        assert {r.id for r in results} == {exact.id}

    def test_superseded_confidence_does_not_qualify(self, make_kb: KbFactory) -> None:
        """A high-confidence assertion that has since been retracted must
        not count - entities_meeting_confidence only considers
        status='active' assertions, matching the pre-KI-028 assertions()
        call's own default."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=TRUSTED)
        high = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.9)
        kb.retract(high.id, TRUSTED)

        assert kb.query("Person").min_confidence(0.5).all() == []

    def test_other_concept_does_not_leak_in(self, make_kb: KbFactory) -> None:
        """A high-confidence assertion on an Organization entity must not
        surface when querying Person - entities_meeting_confidence is
        scoped by concept, not just namespace."""
        kb = _kb(make_kb)
        org = kb.create_entity("Organization", author=TRUSTED)
        kb.assert_literal(org.id, "Organization.name", "Acme", "Text", TRUSTED, confidence=0.9)

        assert kb.query("Person").min_confidence(0.5).all() == []


class TestTrustAtLeast:
    def test_filters_by_author_trust_level(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        high_trust_entity = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(high_trust_entity.id, "Person.name", "Ada", "Text", TRUSTED)

        low_trust_entity = kb.create_entity("Person", author=UNTRUSTED)
        kb.assert_literal(low_trust_entity.id, "Person.name", "Grace", "Text", UNTRUSTED)

        results = kb.query("Person").trust_at_least(5).all()
        assert {r.id for r in results} == {high_trust_entity.id}

    def test_empty_concept_returns_empty(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        assert kb.query("Person").trust_at_least(5).all() == []

    def test_boundary_trust_level_is_inclusive(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", TRUSTED)

        # TRUSTED has trust_level=8; querying at exactly 8 must still match.
        results = kb.query("Person").trust_at_least(8).all()
        assert {r.id for r in results} == {entity.id}

    def test_superseded_trust_does_not_qualify(self, make_kb: KbFactory) -> None:
        """A trusted author's assertion that has since been retracted must
        not count - entities_meeting_trust only considers status='active'
        assertions."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=TRUSTED)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", TRUSTED)
        kb.retract(assertion.id, TRUSTED)

        assert kb.query("Person").trust_at_least(5).all() == []

    def test_other_concept_does_not_leak_in(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        org = kb.create_entity("Organization", author=TRUSTED)
        kb.assert_literal(org.id, "Organization.name", "Acme", "Text", TRUSTED)

        assert kb.query("Person").trust_at_least(5).all() == []


class TestConfidenceAndTrustCombined:
    def test_filters_are_independent(self, make_kb: KbFactory) -> None:
        """Neither filter requires the *same* assertion to satisfy both (ADR-0020 amendment)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.1)
        kb.assert_literal(entity.id, "Person.born", "1815", "Text", UNTRUSTED, confidence=0.95)

        results = kb.query("Person").min_confidence(0.5).trust_at_least(5).all()
        assert {r.id for r in results} == {entity.id}

    def test_filters_are_conjunctive_not_disjunctive(self, make_kb: KbFactory) -> None:
        """Chaining both is an AND, not an OR: an entity qualifying on only
        one of the two filters must be excluded."""
        kb = _kb(make_kb)
        confident_but_untrusted = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(
            confident_but_untrusted.id,
            "Person.name",
            "Ada",
            "Text",
            UNTRUSTED,
            confidence=0.9,
        )

        trusted_but_unconfident = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(
            trusted_but_unconfident.id,
            "Person.name",
            "Grace",
            "Text",
            TRUSTED,
            confidence=0.1,
        )

        neither = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(neither.id, "Person.name", "Bob", "Text", UNTRUSTED, confidence=0.1)

        results = kb.query("Person").min_confidence(0.5).trust_at_least(5).all()
        assert results == []

    def test_combined_with_where_narrows_candidates_first(self, make_kb: KbFactory) -> None:
        """.where()'s narrowed candidate set is intersected with the
        confidence/trust push-down's (namespace, concept)-wide qualifying
        set - a concept-wide match that .where() already excluded must not
        reappear in the final result."""
        kb = _kb(make_kb)
        matching_high_conf = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(
            matching_high_conf.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.9
        )

        matching_low_conf = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(
            matching_low_conf.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.1
        )

        nonmatching_high_conf = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(
            nonmatching_high_conf.id, "Person.name", "Grace", "Text", TRUSTED, confidence=0.9
        )

        results = kb.query("Person").where(name="Ada").min_confidence(0.5).all()
        assert {r.id for r in results} == {matching_high_conf.id}


class TestAsOfConfidenceTrust:
    """KI-036: .min_confidence()/.trust_at_least() must respect .as_of() -
    a query pinned to time t must evaluate against a coherent point-in-time
    view, not always against current-active assertions/principals regardless
    of t."""

    def test_min_confidence_as_of_before_retraction_still_qualifies(
        self, make_kb: KbFactory
    ) -> None:
        """Before the fix, .as_of(t) was ignored entirely by .min_confidence()
        - a query pinned to a time before a later retraction would wrongly
        see the retraction's effect early (status='active' is checked, not
        the bitemporal window)."""
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        high = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.9)
        before_retraction = clock.now()

        clock.advance(days=1)
        kb.retract(high.id, TRUSTED)

        assert kb.query("Person").min_confidence(0.5).all() == []
        results = kb.as_of(before_retraction).query("Person").min_confidence(0.5).all()
        assert {r.id for r in results} == {entity.id}

    def test_trust_at_least_as_of_before_retraction_still_qualifies(
        self, make_kb: KbFactory
    ) -> None:
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", TRUSTED)
        before_retraction = clock.now()

        clock.advance(days=1)
        kb.retract(assertion.id, TRUSTED)

        assert kb.query("Person").trust_at_least(5).all() == []
        results = kb.as_of(before_retraction).query("Person").trust_at_least(5).all()
        assert {r.id for r in results} == {entity.id}

    def test_min_confidence_as_of_respects_supersession_window(self, make_kb: KbFactory) -> None:
        """Person.employer is time_varying (SPEC §10.2): a second value
        supersedes the first rather than contradicting it. .min_confidence()
        pinned to a time inside the first assertion's window must see the
        first assertion's confidence - not the second's, which is active
        now but did not exist yet at that time."""
        kb = _kb_time_varying(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(entity.id, "Person.employer", "Acme", "Text", TRUSTED, confidence=0.9)
        mid = clock.now() + timedelta(hours=12)

        clock.advance(days=1)
        kb.assert_literal(entity.id, "Person.employer", "Beta", "Text", TRUSTED, confidence=0.1)

        results_mid = kb.as_of(mid).query("Person").min_confidence(0.5).all()
        assert {r.id for r in results_mid} == {entity.id}

        assert kb.query("Person").min_confidence(0.5).all() == []

    def test_trust_at_least_as_of_respects_supersession_window(self, make_kb: KbFactory) -> None:
        """Same shape as the confidence version above, but the two
        supersession-chain assertions come from differently-trusted authors
        - the qualifying assertion at each point in time is the one active
        then, not whichever happens to be active now."""
        kb = _kb_time_varying(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(entity.id, "Person.employer", "Acme", "Text", TRUSTED)
        mid = clock.now() + timedelta(hours=12)

        clock.advance(days=1)
        kb.assert_literal(entity.id, "Person.employer", "Beta", "Text", UNTRUSTED)

        results_mid = kb.as_of(mid).query("Person").trust_at_least(5).all()
        assert {r.id for r in results_mid} == {entity.id}

        assert kb.query("Person").trust_at_least(5).all() == []

    def test_min_confidence_as_of_excludes_assertion_not_yet_known(
        self, make_kb: KbFactory
    ) -> None:
        """Isolates the `asserted_at` clause from `valid_from`: a fact
        backdated to before t (`valid_from <= t`) still must not be visible
        at `.as_of(t)` if we didn't learn of it (`asserted_at`) until after
        t - the two temporal dimensions are independent."""
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        t = clock.now()

        clock.advance(days=2)
        kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.9, valid_from=t
        )

        assert kb.as_of(t).query("Person").min_confidence(0.5).all() == []
        results_now = kb.query("Person").min_confidence(0.5).all()
        assert {r.id for r in results_now} == {entity.id}

    def test_min_confidence_as_of_excludes_future_valid_from(self, make_kb: KbFactory) -> None:
        """Isolates the `valid_from` clause from `asserted_at`: asserted at
        t itself (`asserted_at <= t` trivially holds), but the fact doesn't
        become true until later - `.as_of(t)` must not see it yet."""
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        t = clock.now()
        future_valid_from = t + timedelta(days=5)

        kb.assert_literal(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            TRUSTED,
            confidence=0.9,
            valid_from=future_valid_from,
        )

        assert kb.as_of(t).query("Person").min_confidence(0.5).all() == []
        results = kb.as_of(future_valid_from).query("Person").min_confidence(0.5).all()
        assert {r.id for r in results} == {entity.id}

    def test_min_confidence_as_of_excludes_at_valid_to_boundary(self, make_kb: KbFactory) -> None:
        """Half-open interval (valid_from <= t < valid_to): `.as_of()`
        pinned exactly at `valid_to` must exclude; one second earlier must
        still include."""
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        t0 = clock.now()
        valid_to = t0 + timedelta(days=1)
        kb.assert_literal(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            TRUSTED,
            confidence=0.9,
            valid_from=t0,
            valid_to=valid_to,
        )

        just_before = valid_to - timedelta(seconds=1)
        results = kb.as_of(just_before).query("Person").min_confidence(0.5).all()
        assert {r.id for r in results} == {entity.id}
        assert kb.as_of(valid_to).query("Person").min_confidence(0.5).all() == []

    def test_min_confidence_as_of_excludes_flagged_assertion(self, make_kb: KbFactory) -> None:
        """A high-confidence assertion that was active and undisputed at t
        but is flagged as part of a contradiction later must still be
        excluded at `.as_of(t)` - `status` is not itself bitemporally
        versioned; the flagged check always reflects current status."""
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        first = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.9)
        t = clock.now()

        clock.advance(days=1)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", TRUSTED, confidence=0.9)

        reloaded = kb.backend.get_assertion(first.id)
        assert reloaded is not None
        assert reloaded.status == "flagged"

        assert kb.as_of(t).query("Person").min_confidence(0.5).all() == []

    def test_trust_at_least_as_of_excludes_assertion_not_yet_known(
        self, make_kb: KbFactory
    ) -> None:
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        t = clock.now()

        clock.advance(days=2)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", TRUSTED, valid_from=t)

        assert kb.as_of(t).query("Person").trust_at_least(5).all() == []
        results_now = kb.query("Person").trust_at_least(5).all()
        assert {r.id for r in results_now} == {entity.id}

    def test_trust_at_least_as_of_excludes_future_valid_from(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        t = clock.now()
        future_valid_from = t + timedelta(days=5)

        kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", TRUSTED, valid_from=future_valid_from
        )

        assert kb.as_of(t).query("Person").trust_at_least(5).all() == []
        results = kb.as_of(future_valid_from).query("Person").trust_at_least(5).all()
        assert {r.id for r in results} == {entity.id}

    def test_trust_at_least_as_of_excludes_at_valid_to_boundary(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        t0 = clock.now()
        valid_to = t0 + timedelta(days=1)
        kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", TRUSTED, valid_from=t0, valid_to=valid_to
        )

        just_before = valid_to - timedelta(seconds=1)
        results = kb.as_of(just_before).query("Person").trust_at_least(5).all()
        assert {r.id for r in results} == {entity.id}
        assert kb.as_of(valid_to).query("Person").trust_at_least(5).all() == []

    def test_trust_at_least_as_of_excludes_flagged_assertion(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        first = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", TRUSTED)
        t = clock.now()

        clock.advance(days=1)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", TRUSTED)

        reloaded = kb.backend.get_assertion(first.id)
        assert reloaded is not None
        assert reloaded.status == "flagged"

        assert kb.as_of(t).query("Person").trust_at_least(5).all() == []

    def test_as_of_min_confidence_combined_with_where(self, make_kb: KbFactory) -> None:
        """The exact regression shape this KI describes: an entity passes
        `.where()` both at t and now, but only qualifies on confidence at t
        - the confidence check, not just the candidate set, must respect
        `.as_of()`."""
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", TRUSTED)
        born = kb.assert_literal(entity.id, "Person.born", "1815", "Text", TRUSTED, confidence=0.9)
        before_retraction = clock.now()

        clock.advance(days=1)
        kb.retract(born.id, TRUSTED)

        assert kb.query("Person").where(name="Ada").min_confidence(0.5).all() == []
        results = (
            kb.as_of(before_retraction).query("Person").where(name="Ada").min_confidence(0.5).all()
        )
        assert {r.id for r in results} == {entity.id}

    def test_as_of_trust_at_least_combined_with_where(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", UNTRUSTED)
        born = kb.assert_literal(entity.id, "Person.born", "1815", "Text", TRUSTED)
        before_retraction = clock.now()

        clock.advance(days=1)
        kb.retract(born.id, TRUSTED)

        assert kb.query("Person").where(name="Ada").trust_at_least(5).all() == []
        results = (
            kb.as_of(before_retraction).query("Person").where(name="Ada").trust_at_least(5).all()
        )
        assert {r.id for r in results} == {entity.id}

    def test_as_of_min_confidence_and_trust_at_least_intersect_across_boundary(
        self, make_kb: KbFactory
    ) -> None:
        """The `qualifying_ids & trust_ids` intersection (ADR-0020
        amendment) must also respect `.as_of()`: both qualifying assertions
        were active at t; only the confidence one survives to now."""
        kb = _kb(make_kb)
        clock = kb.clock
        assert isinstance(clock, FixedClock)
        entity = kb.create_entity("Person", author=TRUSTED)
        confident = kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.9
        )
        kb.assert_literal(entity.id, "Person.born", "1815", "Text", TRUSTED, confidence=0.1)
        t = clock.now()

        clock.advance(days=1)
        kb.retract(confident.id, TRUSTED)

        assert kb.query("Person").min_confidence(0.5).trust_at_least(5).all() == []
        results = kb.as_of(t).query("Person").min_confidence(0.5).trust_at_least(5).all()
        assert {r.id for r in results} == {entity.id}


class TestCandidateIdsNarrowing:
    """KI-037: `entities_meeting_confidence`/`entities_meeting_trust` accept
    an optional `candidate_ids` narrowing hint on top of `(namespace,
    concept)`. A backend MAY ignore it (DuckDB does - see its docstring),
    so cross-backend vectors here only pin the invariant that holds either
    way: candidate_ids never resurrects a non-qualifying entity, and the
    end-to-end `QueryBuilder` result is unaffected by whichever backend is
    under test. SQLite-specific "does narrowing actually narrow" vectors
    live in `tests/unit/test_sqlite_backend.py`, since that's the one
    backend that implements it."""

    def test_entities_meeting_confidence_candidate_not_qualifying_is_excluded(
        self, make_kb: KbFactory
    ) -> None:
        """Narrowing to a candidate that doesn't itself meet the threshold
        must not resurrect it - candidate_ids restricts scope, it doesn't
        override the threshold check."""
        kb = _kb(make_kb)
        low = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(low.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.1)

        narrowed = kb.backend.entities_meeting_confidence(
            "default", "Person", 0.5, candidate_ids=frozenset({low.id})
        )
        assert narrowed == set()

    def test_where_narrowed_min_confidence_excludes_higher_confidence_non_match(
        self, make_kb: KbFactory
    ) -> None:
        """End-to-end via QueryBuilder: a higher-confidence entity that
        .where() already excluded must not reappear just because
        candidate_ids-based narrowing (or a backend ignoring it) touches
        the SQL differently than the unnarrowed path."""
        kb = _kb(make_kb)
        matching = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(matching.id, "Person.name", "Ada", "Text", TRUSTED, confidence=0.5)
        nonmatching_higher_confidence = kb.create_entity("Person", author=TRUSTED)
        kb.assert_literal(
            nonmatching_higher_confidence.id,
            "Person.name",
            "Grace",
            "Text",
            TRUSTED,
            confidence=0.99,
        )

        results = kb.query("Person").where(name="Ada").min_confidence(0.5).all()
        assert {r.id for r in results} == {matching.id}
