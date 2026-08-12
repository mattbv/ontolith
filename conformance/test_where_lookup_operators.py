"""Conformance vectors for QueryBuilder.where()'s lookup operators (KI-039).

`.where()` accepts a closed set of dunder-suffixed lookup operators on top
of plain equality: `__contains` (substring, SQLite `LIKE ESCAPE`/DuckDB
`LIKE ESCAPE`) and `__gt`/`__lt`/`__gte`/`__lte` (numeric range, via
`CAST(value_lit AS REAL)` on SQLite and `CAST(value_lit AS DOUBLE)` on
DuckDB). This file proves both backends implement every operator
identically, since tests/unit/test_query.py only ever exercises SQLite
directly (Ontology.connect() always builds a SQLiteBackend).
"""

from __future__ import annotations

from datetime import UTC, datetime

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.schema import ConceptDef, PropertyDef, SchemaIR

T0 = datetime(2025, 1, 1, tzinfo=UTC)

ADMIN = "admin@example.com"
AUTHOR = "author@example.com"


def _kb(make_kb: KbFactory) -> Ontology:
    """Schema declaring Person.name (Text), Person.age (Integer)."""
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(20)])
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
                        "age": PropertyDef(name="age", value_type="Integer"),
                    },
                ),
            },
        ),
        author=ADMIN,
    )
    return kb


class TestContains:
    def test_matches_substring(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        matching = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(
            matching.id, "Person.name", "Ada Lovelace, computer scientist", "Text", AUTHOR
        )
        nonmatching = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(nonmatching.id, "Person.name", "Grace Hopper", "Text", AUTHOR)

        results = kb.query("Person").where(name__contains="Lovelace").all()

        assert {r.id for r in results} == {matching.id}

    def test_treats_like_wildcards_literally(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        percent = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(percent.id, "Person.name", "100% Ada", "Text", AUTHOR)
        other = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(other.id, "Person.name", "no wildcards here", "Text", AUTHOR)

        assert {r.id for r in kb.query("Person").where(name__contains="100%").all()} == {percent.id}
        assert kb.query("Person").where(name__contains="_").all() == []

    def test_no_match_returns_empty(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)

        assert kb.query("Person").where(name__contains="nonexistent").all() == []


class TestRangeOperators:
    def test_gt_lt_gte_lte_boundaries(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        exact = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(exact.id, "Person.age", "30", "Integer", AUTHOR)

        assert {r.id for r in kb.query("Person").where(age__gte=30).all()} == {exact.id}
        assert kb.query("Person").where(age__gt=30).all() == []
        assert {r.id for r in kb.query("Person").where(age__lte=30).all()} == {exact.id}
        assert kb.query("Person").where(age__lt=30).all() == []

    def test_numeric_comparison_not_lexicographic(self, make_kb: KbFactory) -> None:
        """ "9" > "10" lexicographically but not numerically - proves the
        comparison is CAST to a number, not compared as text (the whole
        point of restricting this operator to Integer/Float predicates)."""
        kb = _kb(make_kb)
        nine = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(nine.id, "Person.age", "9", "Integer", AUTHOR)
        ten = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(ten.id, "Person.age", "10", "Integer", AUTHOR)

        results = kb.query("Person").where(age__gt=9).all()

        assert {r.id for r in results} == {ten.id}

    def test_composes_as_and_across_two_calls(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        in_range = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(in_range.id, "Person.age", "25", "Integer", AUTHOR)
        too_young = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(too_young.id, "Person.age", "10", "Integer", AUTHOR)
        too_old = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(too_old.id, "Person.age", "40", "Integer", AUTHOR)

        results = kb.query("Person").where(age__gte=20).where(age__lt=40).all()

        assert {r.id for r in results} == {in_range.id}
