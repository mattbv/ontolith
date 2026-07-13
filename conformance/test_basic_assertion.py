"""Conformance vector: Basic assertion creation and retrieval.

SPEC §5.3, §7 - Assertions are append-only with provenance.
"""

from datetime import UTC, datetime

from conformance.conftest import KbFactory
from ontolith import FixedClock, SequentialIdProvider


def test_basic_assertion_create_and_retrieve(make_kb: KbFactory) -> None:
    """Basic assertion flow: create entity, assert value, retrieve.

    SPEC §5.2, §5.3: Entities carry no values directly; all attributes
    are assertions with full provenance.
    """
    # Setup deterministic environment
    clock = FixedClock("2025-01-01T00:00:00Z")
    ids = SequentialIdProvider(prefix="test")

    # Open knowledge base
    kb = make_kb(clock, ids)

    # Create principal
    alice = kb.create_principal("alice@test.com", kind="human", default_capability="write")
    assert alice.id == "alice@test.com"

    # Create entity
    entity = kb.create_entity(concept="Person", author=alice.id, natural_key="ada")
    assert entity.id == "test-001"  # First ID from SequentialIdProvider
    assert entity.concept == "Person"
    assert entity.natural_key == "ada"

    # Assert property
    assertion = kb.assert_literal(
        subject=entity.id,
        predicate="Person.name",
        value="Ada Lovelace",
        value_type="Text",
        author=alice.id,
        source="test",
        confidence=1.0,
    )

    # Verify assertion structure
    assert assertion.id == "test-002"  # Second ID
    assert assertion.subject == entity.id
    assert assertion.predicate == "Person.name"
    assert assertion.value == "Ada Lovelace"
    assert assertion.value_kind == "literal"
    assert assertion.value_type == "Text"
    assert assertion.author == alice.id
    assert assertion.confidence == 1.0
    assert assertion.status == "active"
    assert assertion.asserted_at == datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    assert assertion.valid_from == datetime(2025, 1, 1, 0, 0, tzinfo=UTC)

    # Retrieve assertions
    assertions = kb.assertions(subject=entity.id)
    assert len(assertions) == 1
    assert assertions[0].value == "Ada Lovelace"
    assert assertions[0].id == assertion.id

    kb.close()


def test_assertion_append_only_invariant(make_kb: KbFactory) -> None:
    """Assertions are append-only: value field never mutates.

    SPEC §7: Only status, valid_to, and supersedes links can be modified.
    """
    # Setup
    clock = FixedClock("2025-01-01T00:00:00Z")
    ids = SequentialIdProvider(prefix="test")

    kb = make_kb(clock, ids)
    alice = kb.create_principal("alice@test.com", kind="human", default_capability="write")
    entity = kb.create_entity(concept="Person", author=alice.id)

    # Create assertion
    assertion = kb.assert_literal(
        subject=entity.id,
        predicate="Person.name",
        value="Ada",
        value_type="Text",
        author=alice.id,
    )

    # Verify immutability - assertion value cannot be changed
    from pydantic import ValidationError

    try:
        assertion.value = "Grace"  # type: ignore
        raise AssertionError("Should have raised ValidationError")
    except ValidationError:
        pass  # Expected - assertions are frozen

    # Only allowed mutation: status update via backend
    kb.backend.set_assertion_status(assertion.id, "retracted")

    # Retrieve and verify status changed but value unchanged
    assertions = kb.assertions(subject=entity.id, status=None)
    assert len(assertions) == 1
    assert assertions[0].value == "Ada"  # Value unchanged
    assert assertions[0].status == "retracted"  # Status changed

    kb.close()
