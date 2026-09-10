"""Conformance vectors for `QueryBuilder.include_flagged()` /
`include_history()` (KI-081, SPEC §11.2 / §10.3).

By default a `.where()` filter matches only `active` assertions, so a
flagged (contradicted), superseded, or retracted value is invisible to
`kb.query(Concept).where(...)`. `.include_flagged()` widens the match set
to also include `flagged`; `.include_history()` also includes `superseded`
and `retracted`. Both are match-set wideners — `.all()` still returns
`list[Entity]`. The two are independent (one does not imply the other).

`tests/unit/test_query.py` only exercises SQLite (`Ontology.connect()`
always builds a `SQLiteBackend`); this file pins the same behavior on
DuckDB too, since the widening lives in each backend's own
`entities_where()`.
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
