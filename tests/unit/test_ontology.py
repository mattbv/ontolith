"""Unit tests for Ontology entry point."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import FixedClock, SequentialIdProvider
from ontolith.core.errors import AuthError, CapabilityError, SchemaError
from ontolith.schema import SchemaIR


@pytest.fixture
def temp_db() -> Path:
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        return Path(f.name)


@pytest.fixture
def kb(temp_db: Path) -> Ontology:
    """Create an Ontology instance with deterministic behavior and a seeded principal."""
    clock = FixedClock("2025-01-01T00:00:00Z")
    ids = SequentialIdProvider(prefix="test")
    ontology = Ontology.connect(temp_db, clock=clock, id_provider=ids)
    # Pre-create the principal used across tests. Uses its own string ID,
    # not the SequentialIdProvider, so entity/assertion IDs are unaffected.
    ontology.create_principal("alice@example.com", kind="human")
    yield ontology
    ontology.close()
    temp_db.unlink()


class TestOntology:
    """Tests for Ontology class."""

    def test_connect_creates_database(self, temp_db: Path) -> None:
        """Connecting creates the database and schema."""
        kb = Ontology.connect(temp_db)
        assert temp_db.exists()
        kb.close()

    def test_create_entity(self, kb: Ontology) -> None:
        """Entities can be created."""
        entity = kb.create_entity(
            concept="Person",
            author="alice@example.com",
            natural_key="ada",
        )

        assert entity.id == "test-001"  # Sequential ID
        assert entity.concept == "Person"
        assert entity.natural_key == "ada"
        assert entity.namespace == "default"
        assert entity.created_by == "alice@example.com"
        assert entity.created_at == datetime(2025, 1, 1, tzinfo=UTC)

    def test_create_entity_persists(self, kb: Ontology) -> None:
        """Created entities are persisted."""
        entity = kb.create_entity(
            concept="Person",
            author="alice@example.com",
        )

        retrieved = kb.get_entity(entity.id)
        assert retrieved is not None
        assert retrieved.id == entity.id

    def test_assert_literal(self, kb: Ontology) -> None:
        """Literal assertions can be created."""
        entity = kb.create_entity("Person", author="alice@example.com")

        assertion = kb.assert_literal(
            subject=entity.id,
            predicate="Person.name",
            value="Ada Lovelace",
            value_type="Text",
            author="alice@example.com",
            confidence=0.95,
            source="test",
        )

        assert assertion.id == "test-002"  # Entity was test-001
        assert assertion.subject == entity.id
        assert assertion.predicate == "Person.name"
        assert assertion.value_kind == "literal"
        assert assertion.value_type == "Text"
        assert assertion.value == "Ada Lovelace"
        assert assertion.confidence == 0.95
        assert assertion.source == "test"

    def test_assert_ref(self, kb: Ontology) -> None:
        """Reference assertions can be created."""
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        assertion = kb.assert_ref(
            subject=person.id,
            predicate="Person.employer",
            target=org.id,
            author="alice@example.com",
            confidence=1.0,
        )

        assert assertion.value_kind == "ref"
        assert assertion.value_type is None  # No type for refs
        assert assertion.value == org.id

    def test_query_assertions_by_subject(self, kb: Ontology) -> None:
        """Assertions can be queried by subject."""
        entity = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", author="alice@example.com")
        kb.assert_literal(
            entity.id, "Person.born", "1815-12-10", "Date", author="alice@example.com"
        )

        assertions = kb.assertions(subject=entity.id)
        assert len(assertions) == 2
        assert {a.predicate for a in assertions} == {"Person.name", "Person.born"}

    def test_query_assertions_by_predicate(self, kb: Ontology) -> None:
        """Assertions can be queried by predicate."""
        e1 = kb.create_entity("Person", author="alice@example.com")
        e2 = kb.create_entity("Person", author="alice@example.com")

        kb.assert_literal(e1.id, "Person.name", "Ada", "Text", author="alice@example.com")
        kb.assert_literal(e2.id, "Person.name", "Grace", "Text", author="alice@example.com")

        assertions = kb.assertions(predicate="Person.name")
        assert len(assertions) == 2
        assert {a.value for a in assertions} == {"Ada", "Grace"}

    def test_assertions_default_to_active_only(self, kb: Ontology) -> None:
        """Assertions query defaults to active status only."""
        entity = kb.create_entity("Person", author="alice@example.com")
        assertion = kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", author="alice@example.com"
        )

        # Retract it
        kb.backend.set_assertion_status(assertion.id, "retracted")

        # Default query doesn't return retracted
        active = kb.assertions(subject=entity.id)
        assert len(active) == 0

        # Explicit status=None returns all
        all_assertions = kb.assertions(subject=entity.id, status=None)
        assert len(all_assertions) == 1
        assert all_assertions[0].status == "retracted"

    def test_end_to_end_workflow(self, kb: Ontology) -> None:
        """Complete workflow: create entity, assert properties, query."""
        # Create entity
        person = kb.create_entity(
            concept="Person",
            author="alice@example.com",
            natural_key="ada",
        )

        # Make assertions
        kb.assert_literal(
            person.id,
            "Person.name",
            "Ada Lovelace",
            "Text",
            author="alice@example.com",
            source="Wikipedia",
            confidence=1.0,
        )

        kb.assert_literal(
            person.id,
            "Person.born",
            "1815-12-10",
            "Date",
            author="alice@example.com",
            confidence=1.0,
        )

        # Query by natural key (simulate)
        retrieved = kb.get_entity(person.id)
        assert retrieved is not None
        assert retrieved.natural_key == "ada"

        # Get all facts about the person
        facts = kb.assertions(subject=person.id)
        assert len(facts) == 2

        # Verify provenance
        name_assertion = next(a for a in facts if a.predicate == "Person.name")
        assert name_assertion.author == "alice@example.com"
        assert name_assertion.source == "Wikipedia"
        assert name_assertion.confidence == 1.0

    def test_create_principal(self, kb: Ontology) -> None:
        """Principals can be created."""
        principal = kb.create_principal(
            "bob@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            trust_level=10,
        )

        assert principal.id == "bob@example.com"
        assert principal.kind == "human"
        assert principal.default_capability == "write"
        assert principal.trust_level == 10

    def test_create_principal_persists(self, kb: Ontology) -> None:
        """Created principals are persisted."""
        kb.create_principal("bob@example.com", kind="human")

        retrieved = kb.get_principal("bob@example.com")
        assert retrieved is not None
        assert retrieved.id == "bob@example.com"

    def test_create_ai_principal_with_owner(self, kb: Ontology) -> None:
        """AI principals require an owner."""
        principal = kb.create_principal(
            "research-bot",
            kind="ai",
            owner="alice@example.com",
            auth_method="workload",
            metadata={"model": "claude-sonnet-4"},
        )

        assert principal.kind == "ai"
        assert principal.owner == "alice@example.com"
        assert principal.metadata["model"] == "claude-sonnet-4"


class TestApplySchema:
    """Tests for Ontology.apply_schema (SPEC §6, capability-checked schema persistence)."""

    def test_admin_can_apply_first_schema_version(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        schema = SchemaIR(namespace="default", version=1, concepts={})

        applied = kb.apply_schema(schema, author="admin@example.com")

        assert applied.version == 1
        assert kb.backend.get_schema("default") is not None

    def test_non_admin_raises_capability_error(self, kb: Ontology) -> None:
        """alice@example.com defaults to 'propose' capability, not 'admin'."""
        schema = SchemaIR(namespace="default", version=1, concepts={})

        with pytest.raises(CapabilityError, match="lacks admin capability"):
            kb.apply_schema(schema, author="alice@example.com")

    def test_unknown_author_raises_auth_error(self, kb: Ontology) -> None:
        schema = SchemaIR(namespace="default", version=1, concepts={})

        with pytest.raises(AuthError, match="Principal not found"):
            kb.apply_schema(schema, author="nobody@example.com")

    def test_second_version_must_be_monotonic(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.apply_schema(
            SchemaIR(namespace="default", version=1, concepts={}), author="admin@example.com"
        )

        applied = kb.apply_schema(
            SchemaIR(namespace="default", version=2, concepts={}), author="admin@example.com"
        )
        assert applied.version == 2

    def test_skipping_a_version_raises_schema_error(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.apply_schema(
            SchemaIR(namespace="default", version=1, concepts={}), author="admin@example.com"
        )

        with pytest.raises(SchemaError, match="expected 2"):
            kb.apply_schema(
                SchemaIR(namespace="default", version=3, concepts={}), author="admin@example.com"
            )

    def test_first_version_must_be_one(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")

        with pytest.raises(SchemaError, match="expected 1"):
            kb.apply_schema(
                SchemaIR(namespace="default", version=2, concepts={}), author="admin@example.com"
            )
