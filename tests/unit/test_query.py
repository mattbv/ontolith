"""Unit tests for query builder."""

import tempfile
from pathlib import Path

import pytest

from ontolith import Ontology


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
