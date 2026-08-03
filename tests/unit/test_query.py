"""Unit tests for query builder."""

import tempfile
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import LookupEmbedder
from ontolith.core.errors import ValidationError
from ontolith.query import QueryBuilder


@pytest.fixture
def kb() -> Ontology:
    """Create a temporary knowledge base."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)

    kb = Ontology.connect(path)
    yield kb
    kb.close()
    path.unlink()


class TestQueryBuilder:
    """Tests for QueryBuilder."""

    def test_query_all_entities_of_concept(self, kb: Ontology) -> None:
        """Query returns all entities of a concept."""
        # Create principal
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")

        # Create entities
        person1 = kb.create_entity("Person", author=alice.id)
        person2 = kb.create_entity("Person", author=alice.id)
        kb.create_entity("Organization", author=alice.id)

        # Query all Person entities
        people = kb.query("Person").all()

        assert len(people) == 2
        assert {p.id for p in people} == {person1.id, person2.id}

    def test_query_with_filter(self, kb: Ontology) -> None:
        """Query filters by property value."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")

        # Create entities with different names
        ada = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(ada.id, "Person.name", "Ada Lovelace", "Text", alice.id)

        grace = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(grace.id, "Person.name", "Grace Hopper", "Text", alice.id)

        # Query for specific name
        results = kb.query("Person").where(name="Ada Lovelace").all()

        assert len(results) == 1
        assert results[0].id == ada.id

    def test_query_with_multiple_filters(self, kb: Ontology) -> None:
        """Query filters by multiple properties."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")

        # Create entities
        p1 = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(p1.id, "Person.name", "Ada", "Text", alice.id)
        kb.assert_literal(p1.id, "Person.born", "1815", "Text", alice.id)

        p2 = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(p2.id, "Person.name", "Ada", "Text", alice.id)
        kb.assert_literal(p2.id, "Person.born", "1906", "Text", alice.id)

        # Query with both filters
        results = kb.query("Person").where(name="Ada", born="1815").all()

        assert len(results) == 1
        assert results[0].id == p1.id

    def test_query_first(self, kb: Ontology) -> None:
        """Query.first() returns first match."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")

        p1 = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(p1.id, "Person.name", "Ada", "Text", alice.id)

        p2 = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(p2.id, "Person.name", "Ada", "Text", alice.id)

        result = kb.query("Person").where(name="Ada").first()

        assert result is not None
        assert result.id in {p1.id, p2.id}

    def test_query_first_no_match(self, kb: Ontology) -> None:
        """Query.first() returns None when no match."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_entity("Person", author=alice.id)

        result = kb.query("Person").where(name="Nonexistent").first()

        assert result is None

    def test_query_count(self, kb: Ontology) -> None:
        """Query.count() returns number of matches."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")

        for _ in range(3):
            person = kb.create_entity("Person", author=alice.id)
            kb.assert_literal(person.id, "Person.name", "Ada", "Text", alice.id)

        count = kb.query("Person").where(name="Ada").count()

        assert count == 3

    def test_query_no_results(self, kb: Ontology) -> None:
        """Query with no matches returns empty list."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_entity("Organization", author=alice.id)

        results = kb.query("Person").all()

        assert results == []

    def test_query_without_filter(self, kb: Ontology) -> None:
        """Query without filter returns all entities of concept."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")

        p1 = kb.create_entity("Person", author=alice.id)
        p2 = kb.create_entity("Person", author=alice.id)

        results = kb.query("Person").all()

        assert len(results) == 2
        assert {r.id for r in results} == {p1.id, p2.id}

    def test_query_with_relation_filter_matches_target_id(self, kb: Ontology) -> None:
        """.where(relation=target_id) matches on the relation's value_ref (KI-030)."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")

        acme = kb.create_entity("Organization", author=alice.id)
        globex = kb.create_entity("Organization", author=alice.id)

        ada = kb.create_entity("Person", author=alice.id)
        kb.assert_ref(ada.id, "Person.employer", acme.id, alice.id)

        grace = kb.create_entity("Person", author=alice.id)
        kb.assert_ref(grace.id, "Person.employer", globex.id, alice.id)

        results = kb.query("Person").where(employer=acme.id).all()

        assert {r.id for r in results} == {ada.id}

    def test_where_rejects_dunder_relation_traversal_keys(self, kb: Ontology) -> None:
        """.where(employer__name=...) is not supported and must fail loudly, not no-op (KI-030)."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_entity("Person", author=alice.id)

        with pytest.raises(ValidationError, match="employer__name"):
            kb.query("Person").where(employer__name="Acme Corp")


class TestSemanticSearch:
    """Tests for QueryBuilder.semantic(), using LookupEmbedder for exact rank assertions."""

    def _make_kb(self, tmp_path: Path, mapping: dict[str, list[float]]) -> Ontology:
        embedder = LookupEmbedder(mapping, dim=3)
        return Ontology.connect(tmp_path / "semantic.db", embedder=embedder)

    def test_semantic_ranks_nearest_first(self, tmp_path: Path) -> None:
        """.semantic() ranks reindexed entities by ascending distance to the query vector."""
        kb = self._make_kb(
            tmp_path,
            {
                "Ada": [1.0, 0.0, 0.0],
                "Grace": [0.0, 1.0, 0.0],
                "query": [0.9, 0.1, 0.0],
            },
        )
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        ada = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(ada.id, "Person.name", "Ada", "Text", alice.id)
        grace = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(grace.id, "Person.name", "Grace", "Text", alice.id)

        kb.reindex()
        results = kb.query("Person").semantic("query").all()

        assert [r.id for r in results] == [ada.id, grace.id]
        kb.close()

    def test_semantic_excludes_other_concepts_indexed_in_the_same_scope(
        self, tmp_path: Path
    ) -> None:
        """vector scope="entity" spans every concept; .semantic() filters back down to its own."""
        kb = self._make_kb(
            tmp_path,
            {
                "Ada": [1.0, 0.0, 0.0],
                "Acme": [0.9, 0.1, 0.0],
                "query": [1.0, 0.0, 0.0],
            },
        )
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        ada = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(ada.id, "Person.name", "Ada", "Text", alice.id)
        acme = kb.create_entity("Organization", author=alice.id)
        kb.assert_literal(acme.id, "Organization.name", "Acme", "Text", alice.id)

        kb.reindex()
        results = kb.query("Person").semantic("query").all()

        assert [r.id for r in results] == [ada.id]
        kb.close()

    def test_semantic_without_reindex_returns_empty(self, tmp_path: Path) -> None:
        """No vectors have been upserted yet, so .semantic() finds nothing."""
        kb = self._make_kb(tmp_path, {"query": [1.0, 0.0, 0.0]})
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", alice.id)

        results = kb.query("Person").semantic("query").all()

        assert results == []
        kb.close()

    def test_semantic_intersects_with_where_preserving_rank_order(self, tmp_path: Path) -> None:
        """.semantic() + .where() keeps only symbolic matches, in vector rank order."""
        # _entity_text() sorts by predicate then asserted_at: "Person.born" <
        # "Person.name" alphabetically, so text is "{born} {name}".
        kb = self._make_kb(
            tmp_path,
            {
                "1815 Ada": [1.0, 0.0, 0.0],
                "1815 Grace": [0.0, 1.0, 0.0],
                "1900 Bob": [0.95, 0.05, 0.0],
                "query": [0.9, 0.1, 0.0],
            },
        )
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        ada = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(ada.id, "Person.name", "Ada", "Text", alice.id)
        kb.assert_literal(ada.id, "Person.born", "1815", "Text", alice.id)
        bob = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(bob.id, "Person.name", "Bob", "Text", alice.id)
        kb.assert_literal(bob.id, "Person.born", "1900", "Text", alice.id)
        grace = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(grace.id, "Person.name", "Grace", "Text", alice.id)
        kb.assert_literal(grace.id, "Person.born", "1815", "Text", alice.id)

        kb.reindex()
        # Nearest-to-farthest by the mapping above: ada, bob, grace. Filtering
        # to born=1815 keeps ada and grace, and must preserve that order.
        results = kb.query("Person").semantic("query").where(born="1815").all()

        assert [r.id for r in results] == [ada.id, grace.id]
        kb.close()

    def test_semantic_requires_embedder(self, tmp_path: Path) -> None:
        """A hand-constructed QueryBuilder without an Embedder raises clearly."""
        kb = Ontology.connect(tmp_path / "no-embedder.db")
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_entity("Person", author=alice.id)

        builder = QueryBuilder(backend=kb.backend, namespace=kb.namespace, concept="Person")

        with pytest.raises(ValidationError, match="Embedder"):
            builder.semantic("query").all()
        kb.close()

    def test_semantic_respects_limit_via_overfetch(self, tmp_path: Path) -> None:
        """.semantic().limit(n) returns at most n results, nearest first."""
        kb = self._make_kb(
            tmp_path,
            {
                "Ada": [1.0, 0.0, 0.0],
                "Bob": [0.9, 0.1, 0.0],
                "Grace": [0.0, 1.0, 0.0],
                "query": [1.0, 0.0, 0.0],
            },
        )
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        ada = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(ada.id, "Person.name", "Ada", "Text", alice.id)
        bob = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(bob.id, "Person.name", "Bob", "Text", alice.id)
        grace = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(grace.id, "Person.name", "Grace", "Text", alice.id)

        kb.reindex()
        results = kb.query("Person").semantic("query").limit(1).all()

        assert [r.id for r in results] == [ada.id]
        kb.close()


class TestConfidenceAndTrustFilters:
    """Tests for QueryBuilder.min_confidence()/.trust_at_least()."""

    def test_min_confidence_excludes_none_and_below_threshold(self, kb: Ontology) -> None:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")

        high = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(high.id, "Person.name", "Ada", "Text", alice.id, confidence=0.9)

        low = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(low.id, "Person.name", "Grace", "Text", alice.id, confidence=0.3)

        no_confidence = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(no_confidence.id, "Person.name", "Bob", "Text", alice.id)

        results = kb.query("Person").min_confidence(0.5).all()

        assert {r.id for r in results} == {high.id}

    def test_trust_at_least_filters_by_author_trust_level(self, kb: Ontology) -> None:
        trusted = kb.create_principal(
            "trusted@example.com", kind="human", default_capability="write", trust_level=8
        )
        untrusted = kb.create_principal(
            "untrusted@example.com", kind="human", default_capability="write", trust_level=2
        )

        high_trust_entity = kb.create_entity("Person", author=trusted.id)
        kb.assert_literal(high_trust_entity.id, "Person.name", "Ada", "Text", trusted.id)

        low_trust_entity = kb.create_entity("Person", author=untrusted.id)
        kb.assert_literal(low_trust_entity.id, "Person.name", "Grace", "Text", untrusted.id)

        results = kb.query("Person").trust_at_least(5).all()

        assert {r.id for r in results} == {high_trust_entity.id}

    def test_confidence_and_trust_filters_are_independent(self, kb: Ontology) -> None:
        """Neither filter requires the *same* assertion to satisfy both (ADR-0020 amendment)."""
        trusted_low_conf = kb.create_principal(
            "trusted@example.com", kind="human", default_capability="write", trust_level=9
        )
        untrusted_high_conf = kb.create_principal(
            "newcomer@example.com", kind="human", default_capability="write", trust_level=1
        )

        entity = kb.create_entity("Person", author=trusted_low_conf.id)
        kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", trusted_low_conf.id, confidence=0.1
        )
        kb.assert_literal(
            entity.id, "Person.born", "1815", "Text", untrusted_high_conf.id, confidence=0.95
        )

        results = kb.query("Person").min_confidence(0.5).trust_at_least(5).all()

        assert {r.id for r in results} == {entity.id}

    def test_min_confidence_with_no_qualifying_entities_returns_empty(self, kb: Ontology) -> None:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", alice.id, confidence=0.2)

        results = kb.query("Person").min_confidence(0.9).all()

        assert results == []


class TestLimit:
    """Tests for QueryBuilder.limit()."""

    def test_limit_caps_results(self, kb: Ontology) -> None:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        for _ in range(5):
            kb.create_entity("Person", author=alice.id)

        results = kb.query("Person").limit(2).all()

        assert len(results) == 2

    def test_limit_larger_than_result_set_is_a_noop(self, kb: Ontology) -> None:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        p1 = kb.create_entity("Person", author=alice.id)
        p2 = kb.create_entity("Person", author=alice.id)

        results = kb.query("Person").limit(10).all()

        assert {r.id for r in results} == {p1.id, p2.id}
