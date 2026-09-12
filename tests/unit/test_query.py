"""Unit tests for query builder."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import FixedClock, LookupEmbedder
from ontolith.core.errors import ValidationError
from ontolith.query import QueryBuilder
from ontolith.schema import ConceptDef, PropertyDef, RelationDef, SchemaIR


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

    def test_where_rejects_unrecognized_lookup_operator(self, kb: Ontology) -> None:
        """Only the closed __contains/__gt/__lt/__gte/__lte set is recognized
        (KI-039) - anything else still fails loudly, same as relation
        traversal above."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_entity("Person", author=alice.id)

        with pytest.raises(ValidationError, match="name__startswith"):
            kb.query("Person").where(name__startswith="A")


class TestWhereLookupOperators:
    """KI-039: .where()'s __contains/__gt/__lt/__gte/__lte lookup operators."""

    def _kb_with_numeric_schema(self, kb: Ontology) -> tuple[str, str]:
        """Register a schema declaring Person.age as Integer, Person.score
        as Float, Person.name as Text. Returns (admin_id, author_id)."""
        admin = kb.create_principal(
            "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
        )
        author = kb.create_principal(
            "alice@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        kb.apply_schema(
            SchemaIR(
                namespace="default",
                version=1,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={
                            "name": PropertyDef(name="name", value_type="Text"),
                            "age": PropertyDef(name="age", value_type="Integer"),
                            "score": PropertyDef(name="score", value_type="Float"),
                        },
                        relations={
                            "employer": RelationDef(name="employer", target_concept="Organization")
                        },
                    ),
                    "Organization": ConceptDef(name="Organization"),
                },
            ),
            author=admin.id,
        )
        return admin.id, author.id

    def test_contains_matches_substring(self, kb: Ontology) -> None:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        matching = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(
            matching.id, "Person.bio", "Compound X reduces inflammation", "Text", alice.id
        )
        nonmatching = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(nonmatching.id, "Person.bio", "Unrelated finding", "Text", alice.id)

        results = kb.query("Person").where(bio__contains="Compound X").all()

        assert {r.id for r in results} == {matching.id}

    def test_contains_no_match_returns_empty(self, kb: Ontology) -> None:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(entity.id, "Person.bio", "Ada Lovelace", "Text", alice.id)

        assert kb.query("Person").where(bio__contains="nonexistent").all() == []

    def test_contains_treats_like_wildcards_literally(self, kb: Ontology) -> None:
        """A literal `%`/`_` in the search term must not act as a SQL LIKE
        wildcard - `%` unescaped would match anything; `_` unescaped would
        match any single character."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        percent = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(percent.id, "Person.bio", "100% pure", "Text", alice.id)
        other = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(other.id, "Person.bio", "no wildcard characters here", "Text", alice.id)

        assert {r.id for r in kb.query("Person").where(bio__contains="100%").all()} == {percent.id}
        # A bare "_" must not match "other"'s bio as an any-single-char wildcard.
        assert kb.query("Person").where(bio__contains="_").all() == []

    def test_range_operators_boundaries(self, kb: Ontology) -> None:
        _, author = self._kb_with_numeric_schema(kb)
        exact = kb.create_entity("Person", author=author)
        kb.assert_literal(exact.id, "Person.age", "30", "Integer", author)

        assert {r.id for r in kb.query("Person").where(age__gte=30).all()} == {exact.id}
        assert kb.query("Person").where(age__gt=30).all() == []
        assert {r.id for r in kb.query("Person").where(age__lte=30).all()} == {exact.id}
        assert kb.query("Person").where(age__lt=30).all() == []

    def test_range_operators_compose_as_and(self, kb: Ontology) -> None:
        _, author = self._kb_with_numeric_schema(kb)
        young = kb.create_entity("Person", author=author)
        kb.assert_literal(young.id, "Person.age", "10", "Integer", author)
        in_range = kb.create_entity("Person", author=author)
        kb.assert_literal(in_range.id, "Person.age", "25", "Integer", author)
        old = kb.create_entity("Person", author=author)
        kb.assert_literal(old.id, "Person.age", "40", "Integer", author)

        results = kb.query("Person").where(age__gte=20, age__lt=40).all()

        assert {r.id for r in results} == {in_range.id}

    def test_repeated_where_same_property_different_operator_composes(self, kb: Ontology) -> None:
        """.where(age__gte=X).where(age__lt=Y) - separate calls, same
        property, different operators - must AND together, not overwrite."""
        _, author = self._kb_with_numeric_schema(kb)
        in_range = kb.create_entity("Person", author=author)
        kb.assert_literal(in_range.id, "Person.age", "25", "Integer", author)
        out_of_range = kb.create_entity("Person", author=author)
        kb.assert_literal(out_of_range.id, "Person.age", "40", "Integer", author)

        results = kb.query("Person").where(age__gte=20).where(age__lt=40).all()

        assert {r.id for r in results} == {in_range.id}

    def test_repeated_where_same_property_same_operator_overwrites(self, kb: Ontology) -> None:
        """Same (property, operator) pair called twice - last value wins,
        matching plain equality's existing behavior, not AND-ed together
        (which would be unsatisfiable for two different equality values)."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        ada = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(ada.id, "Person.name", "Ada", "Text", alice.id)
        grace = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(grace.id, "Person.name", "Grace", "Text", alice.id)

        results = kb.query("Person").where(name="Ada").where(name="Grace").all()

        assert {r.id for r in results} == {grace.id}

    def test_range_operator_rejects_non_numeric_predicate(self, kb: Ontology) -> None:
        self._kb_with_numeric_schema(kb)

        with pytest.raises(ValidationError, match="Integer or Float"):
            kb.query("Person").where(name__gt="A")

    def test_range_operator_rejects_relation_predicate(self, kb: Ontology) -> None:
        """A relation predicate resolves to no value_type at all
        (SchemaIR.value_type_of() returns None for a RelationDef), so it's
        rejected the same way a non-numeric property is."""
        self._kb_with_numeric_schema(kb)

        with pytest.raises(ValidationError, match="Integer or Float"):
            kb.query("Person").where(employer__gt="org-1")

    def test_range_operator_rejects_when_no_schema_registered(self, kb: Ontology) -> None:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_entity("Person", author=alice.id)

        with pytest.raises(ValidationError, match="no schema is registered"):
            kb.query("Person").where(age__gt=18)

    def test_range_operator_rejects_non_numeric_value(self, kb: Ontology) -> None:
        self._kb_with_numeric_schema(kb)

        with pytest.raises(ValidationError, match="not a number"):
            kb.query("Person").where(age__gt="not-a-number")

    def test_range_operator_rejects_bool_value(self, kb: Ontology) -> None:
        """bool is a subclass of int - float(True) == 1.0 would otherwise
        silently accept a boolean as a real numeric filter."""
        self._kb_with_numeric_schema(kb)

        with pytest.raises(ValidationError, match="not a number"):
            kb.query("Person").where(age__gt=True)

    def test_range_operator_works_on_float_typed_predicate(self, kb: Ontology) -> None:
        _, author = self._kb_with_numeric_schema(kb)
        entity = kb.create_entity("Person", author=author)
        kb.assert_literal(entity.id, "Person.score", "3.7", "Float", author)

        results = kb.query("Person").where(score__gt=3.5).all()

        assert {r.id for r in results} == {entity.id}

    def test_range_operator_accepts_negative_and_decimal_values(self, kb: Ontology) -> None:
        _, author = self._kb_with_numeric_schema(kb)
        entity = kb.create_entity("Person", author=author)
        kb.assert_literal(entity.id, "Person.score", "-2.5", "Float", author)

        assert {r.id for r in kb.query("Person").where(score__lt=-2.0).all()} == {entity.id}
        assert kb.query("Person").where(score__gt=-2.0).all() == []

    def test_contains_rejects_non_string_value(self, kb: Ontology) -> None:
        """Unlike range operators, .where(x__contains=...) had no eager
        value validation at all - a non-str value used to reach the
        storage adapter's LIKE-escaping code and raise a bare
        AttributeError there instead of a clear ValidationError here."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_entity("Person", author=alice.id)

        with pytest.raises(ValidationError, match="requires a string value"):
            kb.query("Person").where(bio__contains=5)

    def test_leading_dunder_key_with_empty_property_name_is_rejected(self, kb: Ontology) -> None:
        """.where(__contains="x") has an empty property name after splitting
        off the operator suffix - silently accepting it would resolve to
        predicate "Person." and match nothing, the exact silent-unreachable
        -predicate shape KI-030 exists to eliminate."""
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        kb.create_entity("Person", author=alice.id)

        with pytest.raises(ValidationError, match="__contains"):
            kb.query("Person").where(**{"__contains": "x"})

    def test_range_operator_validates_against_schema_effective_at_as_of_time(
        self, tmp_path: Path
    ) -> None:
        """.as_of(t).query(...).where(age__gt=...) must validate against the
        schema effective at t (SPEC §11.4, KI-019's get_schema_at()), not
        today's - a predicate retyped since t must be judged by what it was
        declared at t. Uses a FixedClock (not wall-clock time) so the two
        apply_schema() calls land at deterministic, distinct instants."""
        clock = FixedClock("2025-01-01T00:00:00Z")
        kb = Ontology.connect(tmp_path / "as_of_schema.db", clock=clock)
        admin = kb.create_principal(
            "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
        )
        author = kb.create_principal(
            "alice@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        kb.apply_schema(
            SchemaIR(
                namespace="default",
                version=1,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={"age": PropertyDef(name="age", value_type="Text")},
                    ),
                },
            ),
            author=admin.id,
        )
        before_retype = clock.now()
        clock.advance(days=1)

        kb.apply_schema(
            SchemaIR(
                namespace="default",
                version=2,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={"age": PropertyDef(name="age", value_type="Integer")},
                    ),
                },
            ),
            author=admin.id,
        )

        # At `before_retype`, Person.age was still declared Text - a range
        # filter must be rejected against that historical schema, even
        # though it would succeed against today's (Integer) schema.
        with pytest.raises(ValidationError, match="Integer or Float"):
            kb.as_of(before_retype).query("Person").where(age__gt=18)

        # Sanity check: the same filter against current state (post-retype) succeeds.
        entity = kb.create_entity("Person", author=author.id)
        kb.assert_literal(entity.id, "Person.age", "25", "Integer", author.id)
        assert {r.id for r in kb.query("Person").where(age__gt=18).all()} == {entity.id}
        kb.close()


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

    def test_semantic_with_as_of_excludes_entity_not_yet_existing(self, tmp_path: Path) -> None:
        """KI-058: found while wiring .as_of() into MCP's query tool -
        _semantic_candidates() previously ignored as_of_time entirely
        unless .where() was also set, so a semantic-only as_of query
        returned entities that didn't exist yet at that point in time, even
        though they're in the vector index and match. No prior interface
        ever combined .as_of() with .semantic() (REST/GraphQL don't expose
        as_of at all), so this was unreachable in practice until now.

        The not-yet-existing entity ("Ada Jr") is the *nearer* vector match
        - a weaker version of this test with the excluded entity ranked
        second wouldn't catch a bug where as_of-exclusion runs after
        .limit() truncates the ranked list instead of before it."""
        clock = FixedClock("2025-01-01T00:00:00Z")
        embedder = LookupEmbedder(
            {"Ada": [0.9, 0.1, 0.0], "Ada Jr": [1.0, 0.0, 0.0], "query": [1.0, 0.0, 0.0]}, dim=3
        )
        kb = Ontology.connect(tmp_path / "semantic_as_of.db", clock=clock, embedder=embedder)
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        existing = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(existing.id, "Person.name", "Ada", "Text", alice.id)
        kb.reindex()
        as_of_time = clock.now()

        clock.advance(days=1)
        later = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(later.id, "Person.name", "Ada Jr", "Text", alice.id)
        kb.reindex()

        results = kb.as_of(as_of_time).query("Person").semantic("query").limit(1).all()

        assert [r.id for r in results] == [existing.id]
        kb.close()

    def test_semantic_with_as_of_and_where_still_excludes_not_yet_existing(
        self, tmp_path: Path
    ) -> None:
        """The .where()-set branch already threaded as_of_time through
        entities_where() before this KI - this pins that the pre-existing
        behavior held, distinct from the no-.where() gap the sibling test
        above closes."""
        clock = FixedClock("2025-01-01T00:00:00Z")
        embedder = LookupEmbedder(
            {"Ada": [1.0, 0.0, 0.0], "Ada Jr": [0.9, 0.1, 0.0], "query": [1.0, 0.0, 0.0]}, dim=3
        )
        kb = Ontology.connect(tmp_path / "semantic_as_of_where.db", clock=clock, embedder=embedder)
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        existing = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(existing.id, "Person.name", "Ada", "Text", alice.id)
        kb.reindex()
        as_of_time = clock.now()

        clock.advance(days=1)
        later = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(later.id, "Person.name", "Ada Jr", "Text", alice.id)
        kb.reindex()

        results = (
            kb.as_of(as_of_time).query("Person").semantic("query").where(name__contains="Ada").all()
        )

        assert [r.id for r in results] == [existing.id]
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

    def test_semantic_narrowed_min_confidence_excludes_lower_confidence_match(
        self, tmp_path: Path
    ) -> None:
        """Branch-coverage vector, not a KI-037 regression pin: exercises the
        `self._semantic_text is not None` disjunct of
        `_apply_confidence_trust_filters`'s candidate_ids gate, which no
        `.where()`-only vector reaches. Passes regardless of whether
        candidate_ids is threaded through at all - the re-intersection in
        `_apply_confidence_trust_filters` makes this outcome invariant to
        that by construction (confirmed: this test still passes reverted to
        pre-KI-037 `main`). The backend-level `TestCandidateIdsNarrowing`
        vectors in conformance/test_confidence_trust_filters.py,
        tests/unit/test_sqlite_backend.py, and
        tests/unit/test_duckdb_backend.py are what actually regression-pin
        KI-037's mechanism."""
        kb = self._make_kb(
            tmp_path,
            {
                "Ada": [1.0, 0.0, 0.0],
                "Grace": [0.9, 0.1, 0.0],
                "query": [1.0, 0.0, 0.0],
            },
        )
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        ada = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(ada.id, "Person.name", "Ada", "Text", alice.id, confidence=0.9)
        grace = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(grace.id, "Person.name", "Grace", "Text", alice.id, confidence=0.1)

        kb.reindex()
        results = kb.query("Person").semantic("query").min_confidence(0.5).all()

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


class TestIncludeFlaggedAndHistory:
    """KI-081 / SPEC §11.2: `.where()` matches only `active` assertions by
    default; `.include_flagged()` and `.include_history()` widen the match
    set. Both are match-set wideners, not result-shape changes — `.all()`
    still returns `list[Entity]`."""

    def _prep(self, kb: Ontology) -> str:
        """Admin + write principal + a schema with a time_varying
        `Person.role`. Returns the write principal's id."""
        admin = kb.create_principal(
            "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
        )
        alice = kb.create_principal(
            "alice@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
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
            author=admin.id,
        )
        return alice.id

    def _flagged_person(self, kb: Ontology, alice_id: str) -> str:
        """Entity whose `Person.name` has two contradicting (flagged)
        assertions and nothing active. Returns the entity id."""
        person = kb.create_entity("Person", author=alice_id)
        kb.assert_literal(person.id, "Person.name", "Ada", "Text", alice_id)
        kb.assert_literal(person.id, "Person.name", "Ava", "Text", alice_id)
        assert kb.assertions(subject=person.id, predicate="Person.name", status="flagged")
        assert not kb.assertions(subject=person.id, predicate="Person.name", status="active")
        return person.id

    def _superseded_person(self, kb: Ontology, alice_id: str) -> str:
        """Entity whose earlier `Person.role` value ('Engineer') was
        superseded by a later one ('Manager'). Returns the entity id."""
        person = kb.create_entity("Person", author=alice_id)
        kb.assert_literal(person.id, "Person.role", "Engineer", "Text", alice_id)
        kb.assert_literal(person.id, "Person.role", "Manager", "Text", alice_id)
        assert kb.assertions(subject=person.id, predicate="Person.role", status="superseded")
        return person.id

    def _retracted_person(self, kb: Ontology, alice_id: str) -> str:
        """Entity whose only `Person.name` assertion was retracted."""
        person = kb.create_entity("Person", author=alice_id)
        a = kb.assert_literal(person.id, "Person.name", "Ada", "Text", alice_id)
        kb.retract(a.id, alice_id)
        assert kb.assertions(subject=person.id, predicate="Person.name", status="retracted")
        return person.id

    def test_flagged_assertion_is_excluded_by_default(self, kb: Ontology) -> None:
        self._flagged_person(kb, self._prep(kb))
        assert kb.query("Person").where(name="Ada").all() == []

    def test_include_flagged_matches_a_flagged_assertion(self, kb: Ontology) -> None:
        pid = self._flagged_person(kb, self._prep(kb))
        results = kb.query("Person").where(name="Ada").include_flagged().all()
        assert [r.id for r in results] == [pid]

    def test_superseded_assertion_is_excluded_by_default(self, kb: Ontology) -> None:
        self._superseded_person(kb, self._prep(kb))
        assert kb.query("Person").where(role="Engineer").all() == []

    def test_include_history_matches_a_superseded_assertion(self, kb: Ontology) -> None:
        pid = self._superseded_person(kb, self._prep(kb))
        results = kb.query("Person").where(role="Engineer").include_history().all()
        assert [r.id for r in results] == [pid]

    def test_include_history_matches_a_retracted_assertion(self, kb: Ontology) -> None:
        pid = self._retracted_person(kb, self._prep(kb))
        results = kb.query("Person").where(name="Ada").include_history().all()
        assert [r.id for r in results] == [pid]

    def test_include_flagged_alone_does_not_match_superseded(self, kb: Ontology) -> None:
        """The two flags are independent: `.include_flagged()` widens to
        `flagged` only, not `superseded`/`retracted`."""
        self._superseded_person(kb, self._prep(kb))
        assert kb.query("Person").where(role="Engineer").include_flagged().all() == []

    def test_include_history_alone_does_not_match_flagged(self, kb: Ontology) -> None:
        self._flagged_person(kb, self._prep(kb))
        assert kb.query("Person").where(name="Ada").include_history().all() == []

    def test_both_flags_widen_to_every_status(self, kb: Ontology) -> None:
        alice_id = self._prep(kb)
        flagged_pid = self._flagged_person(kb, alice_id)
        superseded_pid = self._superseded_person(kb, alice_id)
        assert [
            r.id
            for r in kb.query("Person").where(name="Ada").include_flagged().include_history().all()
        ] == [flagged_pid]
        assert [
            r.id
            for r in kb.query("Person")
            .where(role="Engineer")
            .include_flagged()
            .include_history()
            .all()
        ] == [superseded_pid]

    def test_flags_are_a_noop_without_a_where_filter(self, kb: Ontology) -> None:
        """No `.where()` -> nothing to match against -> the flags change
        nothing (the query still returns every entity of the concept)."""
        alice_id = self._prep(kb)
        p1 = kb.create_entity("Person", author=alice_id)
        p2 = kb.create_entity("Person", author=alice_id)
        results = kb.query("Person").include_flagged().include_history().all()
        assert {r.id for r in results} == {p1.id, p2.id}

    def test_include_methods_are_chainable(self, kb: Ontology) -> None:
        q = kb.query("Person").include_flagged().include_history()
        assert isinstance(q, QueryBuilder)

    def test_count_and_first_honor_include_flagged(self, kb: Ontology) -> None:
        pid = self._flagged_person(kb, self._prep(kb))
        assert kb.query("Person").where(name="Ada").count() == 0
        assert kb.query("Person").where(name="Ada").include_flagged().count() == 1
        assert kb.query("Person").where(name="Ada").first() is None
        first = kb.query("Person").where(name="Ada").include_flagged().first()
        assert first is not None and first.id == pid

    def test_active_value_is_still_returned_under_each_widener(self, kb: Ontology) -> None:
        """The widener invariant (ADR-0048): the opt-ins *add* statuses to
        the match set, they never remove `active`. Pins against a mutation
        that turns `.include_*()` into a narrower."""
        alice_id = self._prep(kb)
        person = kb.create_entity("Person", author=alice_id)
        kb.assert_literal(person.id, "Person.name", "Ada", "Text", alice_id)  # stays active
        assert kb.assertions(subject=person.id, predicate="Person.name", status="active")

        assert [r.id for r in kb.query("Person").where(name="Ada").all()] == [person.id]
        assert [r.id for r in kb.query("Person").where(name="Ada").include_flagged().all()] == [
            person.id
        ]
        assert [r.id for r in kb.query("Person").where(name="Ada").include_history().all()] == [
            person.id
        ]
        assert [
            r.id
            for r in kb.query("Person").where(name="Ada").include_flagged().include_history().all()
        ] == [person.id]

    def test_widener_composes_with_a_second_where_filter(self, kb: Ontology) -> None:
        """Two `.where()` filters + a widener: both still AND, and the
        widened status set applies to each sub-select (exercises the
        per-filter `match_params` splicing with len > 1)."""
        alice_id = self._prep(kb)
        person = kb.create_entity("Person", author=alice_id)
        kb.assert_literal(person.id, "Person.name", "Ada", "Text", alice_id)
        kb.assert_literal(person.id, "Person.name", "Ava", "Text", alice_id)  # name -> flagged
        kb.assert_literal(person.id, "Person.role", "Engineer", "Text", alice_id)  # active

        other = kb.create_entity("Person", author=alice_id)
        kb.assert_literal(other.id, "Person.name", "Ada", "Text", alice_id)
        kb.assert_literal(other.id, "Person.name", "Ava", "Text", alice_id)  # flagged, but no role

        q = kb.query("Person").where(name="Ada").where(role="Engineer").include_flagged()
        assert [r.id for r in q.all()] == [person.id]  # `other` fails the role filter

    def test_as_of_query_default_excludes_flagged_opt_in_includes(self, kb: Ontology) -> None:
        """`.as_of()` + `.include_flagged()` is newly reachable through the
        fluent API (QueryBuilder never threaded `include_flagged` before
        KI-081). The `as_of` path has always excluded flagged by default;
        the opt-in now works there too."""
        alice_id = self._prep(kb)
        person = self._flagged_person(kb, alice_id)
        # The `kb` fixture uses a wall-clock; pick an instant safely after
        # every assertion's asserted_at/valid_from (and before any valid_to,
        # which are all NULL here).
        t = datetime(2099, 1, 1, tzinfo=UTC)

        assert kb.as_of(t).query("Person").where(name="Ada").all() == []
        assert [
            r.id for r in kb.as_of(t).query("Person").where(name="Ada").include_flagged().all()
        ] == [person]

    def test_semantic_where_honors_include_flagged(self, tmp_path: Path) -> None:
        """The `.semantic()` path intersects vector hits with a symbolic
        `entities_where()` call — the widener must thread through there too."""
        # After name -> flagged, the only active Text value is bio, so
        # `_entity_text` embeds just "bio text" — map that.
        embedder = LookupEmbedder({"bio text": [1.0, 0.0, 0.0], "query": [1.0, 0.0, 0.0]}, dim=3)
        kb = Ontology.connect(tmp_path / "sem.db", embedder=embedder)
        try:
            alice = kb.create_principal(
                "alice@example.com", kind="human", default_capability="write"
            )
            person = kb.create_entity("Person", author=alice.id)
            kb.assert_literal(person.id, "Person.name", "Ada", "Text", alice.id)
            # An extra active assertion so reindex() still embeds the entity
            # after the name goes flagged (reindex embeds active Text only).
            kb.assert_literal(person.id, "Person.bio", "bio text", "Text", alice.id)
            kb.assert_literal(person.id, "Person.name", "Ava", "Text", alice.id)  # -> flagged
            assert kb.assertions(subject=person.id, predicate="Person.name", status="flagged")
            kb.reindex()

            assert kb.query("Person").semantic("query").where(name="Ada").all() == []
            hit = kb.query("Person").semantic("query").where(name="Ada").include_flagged().all()
            assert [r.id for r in hit] == [person.id]
        finally:
            kb.close()


class TestIncludeFlaggedHistoryComposesWithConfidenceAndTrust:
    """KI-093: `.include_flagged()`/`.include_history()` must also widen
    `.min_confidence()`/`.trust_at_least()`'s own "active" filtering
    (`entities_meeting_confidence`/`entities_meeting_trust`), not just
    `.where()`'s — otherwise a no-op floor (`min_confidence(0.0)`,
    `trust_at_least(0)`) silently re-narrows an `.include_history()`/
    `.include_flagged()` result back to `active` only."""

    def _prep(self, kb: Ontology) -> str:
        admin = kb.create_principal(
            "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
        )
        kb.create_principal(
            "alice@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        kb.apply_schema(
            SchemaIR(
                namespace="default",
                version=1,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={"name": PropertyDef(name="name", value_type="Text")},
                    )
                },
            ),
            author=admin.id,
        )
        return "alice@example.com"

    def _retracted_person_with_confidence(
        self, kb: Ontology, alice_id: str, confidence: float
    ) -> str:
        person = kb.create_entity("Person", author=alice_id)
        a = kb.assert_literal(
            person.id, "Person.name", "Ada", "Text", alice_id, confidence=confidence
        )
        kb.retract(a.id, alice_id)
        assert kb.assertions(subject=person.id, predicate="Person.name", status="retracted")
        return person.id

    def test_include_history_min_confidence_zero_is_a_true_noop(self, kb: Ontology) -> None:
        alice_id = self._prep(kb)
        pid = self._retracted_person_with_confidence(kb, alice_id, confidence=0.9)

        without_floor = kb.query("Person").where(name="Ada").include_history().all()
        with_noop_floor = (
            kb.query("Person").where(name="Ada").include_history().min_confidence(0.0).all()
        )
        assert [e.id for e in without_floor] == [pid]
        assert [e.id for e in with_noop_floor] == [pid]

    def test_include_history_trust_at_least_zero_is_a_true_noop(self, kb: Ontology) -> None:
        alice_id = self._prep(kb)
        pid = self._retracted_person_with_confidence(kb, alice_id, confidence=0.9)

        with_noop_floor = (
            kb.query("Person").where(name="Ada").include_history().trust_at_least(0).all()
        )
        assert [e.id for e in with_noop_floor] == [pid]

    def test_include_history_min_confidence_still_filters_below_threshold(
        self, kb: Ontology
    ) -> None:
        """The widening isn't a bypass: a genuinely-failing confidence floor
        still excludes, even under `.include_history()`."""
        alice_id = self._prep(kb)
        self._retracted_person_with_confidence(kb, alice_id, confidence=0.2)

        result = kb.query("Person").where(name="Ada").include_history().min_confidence(0.9).all()
        assert result == []

    def test_include_flagged_min_confidence_zero_is_a_true_noop(self, kb: Ontology) -> None:
        alice_id = self._prep(kb)
        person = kb.create_entity("Person", author=alice_id)
        kb.assert_literal(person.id, "Person.name", "Ada", "Text", alice_id, confidence=0.9)
        kb.assert_literal(person.id, "Person.name", "Ava", "Text", alice_id, confidence=0.9)
        assert kb.assertions(subject=person.id, predicate="Person.name", status="flagged")

        result = kb.query("Person").where(name="Ada").include_flagged().min_confidence(0.0).all()
        assert [e.id for e in result] == [person.id]
