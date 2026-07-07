"""Conformance vector: Append-only assertion invariant.

SPEC §5 — Assertions are append-only. Only status, valid_to, and supersedes
links may mutate. Retracted and superseded assertions MUST be retained and
queryable; they MUST be excluded from default (active-only) results.
"""

import tempfile
from pathlib import Path

from ontolith import Ontology, SequentialIdProvider
from ontolith.core import FixedClock

T0 = "2025-01-01T00:00:00+00:00"


def _kb() -> tuple[Ontology, Path]:
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = Path(f.name)
    f.close()
    kb = Ontology.connect(path, clock=FixedClock(T0), id_provider=SequentialIdProvider("cv"))
    return kb, path


def test_active_assertion_is_returned_by_default() -> None:
    """Active assertions appear in default queries (SPEC §5)."""
    kb, path = _kb()
    try:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author=alice.id)
        a = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", alice.id)

        results = kb.assertions(subject=entity.id)
        assert len(results) == 1
        assert results[0].id == a.id
        assert results[0].status == "active"
    finally:
        kb.close()
        path.unlink()


def test_retracted_assertion_excluded_from_default_query() -> None:
    """Retracted assertions are excluded from default (active-only) queries (SPEC §5)."""
    kb, path = _kb()
    try:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author=alice.id)
        a = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", alice.id)

        kb.backend.set_assertion_status(a.id, "retracted")

        active = kb.assertions(subject=entity.id)
        assert len(active) == 0
    finally:
        kb.close()
        path.unlink()


def test_retracted_assertion_survives_in_history() -> None:
    """Retracted assertions are retained and queryable when history is requested (SPEC §5)."""
    kb, path = _kb()
    try:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author=alice.id)
        a = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", alice.id)

        kb.backend.set_assertion_status(a.id, "retracted")

        all_records = kb.assertions(subject=entity.id, status=None)
        assert len(all_records) == 1
        assert all_records[0].id == a.id
        assert all_records[0].status == "retracted"
    finally:
        kb.close()
        path.unlink()


def test_value_field_is_immutable() -> None:
    """The value field of an assertion cannot be changed in-place (SPEC §5).

    New values create new assertions; old ones are retained.
    """
    kb, path = _kb()
    try:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author=alice.id)

        a1 = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", alice.id)
        a2 = kb.assert_literal(entity.id, "Person.name", "Ada Lovelace", "Text", alice.id)

        all_records = kb.assertions(subject=entity.id, status=None)
        ids = {r.id for r in all_records}
        values = {r.value for r in all_records}

        assert a1.id in ids
        assert a2.id in ids
        assert "Ada" in values
        assert "Ada Lovelace" in values
    finally:
        kb.close()
        path.unlink()


def test_multiple_assertions_coexist_for_same_predicate() -> None:
    """Multiple assertions for the same (subject, predicate) coexist (SPEC §5, corroboration)."""
    kb, path = _kb()
    try:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        bob = kb.create_principal("bob@example.com", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author=alice.id)

        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", alice.id, confidence=0.9)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", bob.id, confidence=0.8)

        results = kb.assertions(subject=entity.id, predicate="Person.name")
        assert len(results) == 2
        authors = {r.author for r in results}
        assert alice.id in authors
        assert bob.id in authors
    finally:
        kb.close()
        path.unlink()


def test_provenance_fields_are_captured() -> None:
    """Every assertion carries full provenance: author, asserted_at, confidence (SPEC §5.4)."""
    kb, path = _kb()
    try:
        alice = kb.create_principal("alice@example.com", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author=alice.id)
        kb.assert_literal(
            entity.id,
            "Person.name",
            "Ada",
            "Text",
            alice.id,
            confidence=0.95,
            source="Wikipedia",
            rationale="Primary source",
        )

        retrieved = kb.assertions(subject=entity.id)[0]
        assert retrieved.author == alice.id
        assert retrieved.asserted_at is not None
        assert retrieved.confidence == 0.95
        assert retrieved.source == "Wikipedia"
        assert retrieved.rationale == "Primary source"
    finally:
        kb.close()
        path.unlink()


def test_assertion_records_survive_connection_close() -> None:
    """Assertions are durable: they survive close and reopen of the connection (ADR-0010)."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = Path(f.name)
    f.close()

    kb1 = Ontology.connect(path, clock=FixedClock(T0), id_provider=SequentialIdProvider("cv"))
    alice = kb1.create_principal("alice@example.com", kind="human", default_capability="write")
    entity = kb1.create_entity("Person", author=alice.id)
    a = kb1.assert_literal(entity.id, "Person.name", "Ada", "Text", alice.id)
    kb1.close()

    kb2 = Ontology.connect(path)
    try:
        results = kb2.assertions(subject=entity.id)
        assert len(results) == 1
        assert results[0].id == a.id
        assert results[0].value == "Ada"
    finally:
        kb2.close()
        path.unlink()
