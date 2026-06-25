"""Unit tests for Entity and Assertion models."""

from datetime import UTC, datetime

import pytest

from ontolith.core import Assertion, Entity


class TestEntity:
    """Tests for Entity model."""

    def test_create_entity(self) -> None:
        """Entity can be created with required fields."""
        entity = Entity(
            id="test-001",
            namespace="test-ns",
            concept="Person",
            natural_key="ada",
            created_at=datetime(2025, 1, 1, 0, 0, tzinfo=UTC),
            created_by="alice@test.com",
        )

        assert entity.id == "test-001"
        assert entity.namespace == "test-ns"
        assert entity.concept == "Person"
        assert entity.natural_key == "ada"

    def test_entity_is_immutable(self) -> None:
        """Entity is frozen (immutable)."""
        entity = Entity(
            id="test-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )

        with pytest.raises(Exception):  # Pydantic raises ValidationError
            entity.concept = "Organization"  # type: ignore


class TestAssertion:
    """Tests for Assertion model."""

    def test_create_literal_assertion(self) -> None:
        """Assertion can be created with literal value."""
        assertion = Assertion(
            id="test-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada Lovelace",
            author="alice@test.com",
            source="test",
            confidence=0.95,
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        )

        assert assertion.id == "test-001"
        assert assertion.value_kind == "literal"
        assert assertion.value_type == "Text"
        assert assertion.value == "Ada Lovelace"
        assert assertion.confidence == 0.95
        assert assertion.status == "active"

    def test_create_ref_assertion(self) -> None:
        """Assertion can be created with entity reference."""
        assertion = Assertion(
            id="test-001",
            namespace="test-ns",
            subject="person-001",
            predicate="Person.employer",
            value_kind="ref",
            value="org-001",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        )

        assert assertion.value_kind == "ref"
        assert assertion.value_type is None  # Not required for refs
        assert assertion.value == "org-001"

    def test_literal_requires_value_type(self) -> None:
        """Literal assertions must have value_type."""
        with pytest.raises(ValueError, match="value_type is required"):
            Assertion(
                id="test-001",
                namespace="test-ns",
                subject="entity-001",
                predicate="Person.name",
                value_kind="literal",
                value_type=None,  # Missing!
                value="Ada Lovelace",
                author="alice@test.com",
                asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
                valid_from=datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_assertion_is_immutable(self) -> None:
        """Assertion is frozen (immutable by default)."""
        assertion = Assertion(
            id="test-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        )

        with pytest.raises(Exception):  # Pydantic raises ValidationError
            assertion.value = "New value"  # type: ignore

    def test_assertion_can_copy_with_status_change(self) -> None:
        """Assertion can be 'mutated' via copy for status/valid_to/supersedes."""
        assertion = Assertion(
            id="test-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        )

        # Simulate supersession via copy
        updated = assertion.model_copy(
            update={
                "status": "superseded",
                "valid_to": datetime(2025, 6, 1, tzinfo=UTC),
            }
        )

        assert updated.status == "superseded"
        assert updated.valid_to == datetime(2025, 6, 1, tzinfo=UTC)
        assert updated.value == "Ada"  # Value unchanged
        assert assertion.status == "active"  # Original unchanged

    def test_confidence_range_validated(self) -> None:
        """Confidence must be between 0.0 and 1.0."""
        with pytest.raises(ValueError):
            Assertion(
                id="test-001",
                namespace="test-ns",
                subject="entity-001",
                predicate="Person.name",
                value_kind="literal",
                value_type="Text",
                value="Ada",
                author="alice@test.com",
                confidence=1.5,  # Invalid!
                asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
                valid_from=datetime(2025, 1, 1, tzinfo=UTC),
            )
