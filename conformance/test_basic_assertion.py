"""Conformance vector: Basic assertion creation and retrieval.

SPEC §5.3, §7 - Assertions are append-only with provenance.
"""


import pytest

# These imports will fail until we implement them - that's expected (TDD)
# from ontolith import Ontology
# from ontolith.core import FixedClock, SequentialIdProvider


@pytest.mark.skip(reason="M1: Not yet implemented")
def test_basic_assertion_create_and_retrieve() -> None:
    """Basic assertion flow: create entity, assert value, retrieve.

    SPEC §5.2, §5.3: Entities carry no values directly; all attributes
    are assertions with full provenance.
    """
    # Setup deterministic environment
    # clock = FixedClock("2025-01-01T00:00:00Z")
    # ids = SequentialIdProvider(prefix="test")

    # # Open knowledge base with simple schema
    # kb = Ontology.connect(
    #     "test-basic",
    #     schema=[],  # Will need schema definition
    #     clock=clock,
    #     id_provider=ids,
    # )

    # # Create principal
    # alice = kb.principal("alice@test.com", kind="human")

    # # Create entity
    # # entity = alice.create_entity(concept="Person", natural_key="ada")

    # # Assert property
    # # assertion = alice.assert_property(
    # #     subject=entity.id,
    # #     predicate="name",
    # #     value="Ada Lovelace",
    # #     source="test",
    # #     confidence=1.0,
    # # )

    # # Verify assertion structure
    # # assert assertion.id == "test-001"
    # # assert assertion.subject == entity.id
    # # assert assertion.predicate == "name"
    # # assert assertion.value == "Ada Lovelace"
    # # assert assertion.author == "alice@test.com"
    # # assert assertion.confidence == 1.0
    # # assert assertion.status == "active"
    # # assert assertion.asserted_at == datetime(2025, 1, 1, 0, 0, tzinfo=UTC)

    # # Retrieve assertion
    # # assertions = kb.get_assertions(subject=entity.id)
    # # assert len(assertions) == 1
    # # assert assertions[0].value == "Ada Lovelace"

    pytest.fail("M1: Assertion model not yet implemented")


@pytest.mark.skip(reason="M1: Not yet implemented")
def test_assertion_append_only_invariant() -> None:
    """Assertions are append-only: value field never mutates.

    SPEC §7: Only status, valid_to, and supersedes links can be modified.
    """
    pytest.fail("M1: Not yet implemented")
