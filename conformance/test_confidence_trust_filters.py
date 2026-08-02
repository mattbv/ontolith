"""Conformance vectors for QueryBuilder.min_confidence()/.trust_at_least() (KI-028).

Both filters are pushed down to StorageBackend.entities_meeting_confidence()/
entities_meeting_trust() as bulk id-set lookups rather than a per-entity
assertions()/get_principal() round trip — this file proves both backends
implement that push-down identically, since tests/unit/test_query.py only
ever exercises the SQLite backend directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider

T0 = datetime(2025, 1, 1, tzinfo=UTC)

TRUSTED = "trusted@example.com"
UNTRUSTED = "untrusted@example.com"


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

    def test_no_candidate_entities_issues_no_query(self, make_kb: KbFactory) -> None:
        """entities_meeting_confidence([]) must short-circuit rather than
        issue a SQL `IN ()`, which is invalid syntax on both backends."""
        kb = _kb(make_kb)
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

    def test_no_candidate_entities_issues_no_query(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
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

    def test_combined_with_where_narrows_candidates_first(self, make_kb: KbFactory) -> None:
        """.where() narrows the candidate set before the confidence/trust
        push-down runs - both filters compose rather than each re-scanning
        the whole concept."""
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
