"""Ontology - main entry point for the knowledge base.

The Ontology class is the primary API surface for users. It wraps the storage
backend and provides high-level methods for entities, assertions, and queries.
"""

from pathlib import Path
from typing import Any

from ontolith.core import (
    Assertion,
    Clock,
    Entity,
    IdProvider,
    SystemClock,
    UlidProvider,
)
from ontolith.identity import Principal
from ontolith.store.base import StorageBackend


class Ontology:
    """Main knowledge base interface.

    This is the primary entry point for interacting with an Ontolith knowledge base.
    It provides methods for creating entities, making assertions, and querying.

    Example:
        >>> kb = Ontology.connect("my-kb.db")
        >>> alice = kb.create_principal("alice@example.com", kind="human")
        >>> entity = kb.create_entity("Person", author=alice.id)
        >>> kb.assert_literal(entity.id, "name", "Ada Lovelace", author=alice.id)
    """

    def __init__(
        self,
        backend: StorageBackend,
        clock: Clock | None = None,
        id_provider: IdProvider | None = None,
    ) -> None:
        """Initialize Ontology with a storage backend.

        Args:
            backend: Storage backend implementation
            clock: Clock for deterministic timestamps (defaults to SystemClock)
            id_provider: ID provider for deterministic IDs (defaults to UlidProvider)
        """
        self.backend = backend
        self.clock = clock or SystemClock()
        self.id_provider = id_provider or UlidProvider()
        self.namespace = "default"  # For M1, single namespace

    @classmethod
    def connect(
        cls,
        path: str | Path,
        *,
        clock: Clock | None = None,
        id_provider: IdProvider | None = None,
    ) -> "Ontology":
        """Connect to a knowledge base.

        Args:
            path: Path to SQLite database file
            clock: Optional clock for deterministic behavior
            id_provider: Optional ID provider for deterministic behavior

        Returns:
            Ontology instance connected to the database
        """
        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(path)
        return cls(backend, clock=clock, id_provider=id_provider)

    def create_principal(
        self,
        principal_id: str,
        kind: str,
        auth_method: str = "oidc",
        *,
        owner: str | None = None,
        default_capability: str = "propose",
        trust_level: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> Principal:
        """Create a new principal.

        Args:
            principal_id: Email (human) or slug (ai/service)
            kind: Type of principal (human, ai, service)
            auth_method: Authentication method (oidc, workload, apikey)
            owner: Required for AI principals - accountable human/team
            default_capability: Default permission level
            trust_level: Base trust score
            metadata: Optional metadata

        Returns:
            Created principal
        """
        principal = Principal(
            id=principal_id,
            kind=kind,  # type: ignore
            owner=owner,
            auth_method=auth_method,  # type: ignore
            default_capability=default_capability,  # type: ignore
            trust_level=trust_level,
            created_at=self.clock.now(),
            metadata=metadata or {},
        )

        self.backend.put_principal(principal)
        return principal

    def get_principal(self, principal_id: str) -> Principal | None:
        """Retrieve a principal by ID.

        Args:
            principal_id: Principal ID

        Returns:
            Principal if found, None otherwise
        """
        return self.backend.get_principal(principal_id)

    def create_entity(
        self,
        concept: str,
        author: str,
        natural_key: str | None = None,
    ) -> Entity:
        """Create a new entity.

        Args:
            concept: Concept name (e.g., "Person", "Organization")
            author: Principal ID creating this entity
            natural_key: Optional unique key within concept

        Returns:
            Created entity
        """
        entity = Entity(
            id=self.id_provider.next(),
            namespace=self.namespace,
            concept=concept,
            natural_key=natural_key,
            created_at=self.clock.now(),
            created_by=author,
        )

        self.backend.put_entity(entity)
        return entity

    def assert_literal(
        self,
        subject: str,
        predicate: str,
        value: str,
        value_type: str,
        author: str,
        *,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
    ) -> Assertion:
        """Make a literal assertion about an entity.

        Args:
            subject: Entity ID
            predicate: Property name (e.g., "Person.name")
            value: Literal value
            value_type: Type of literal (Text, Integer, etc.)
            author: Principal ID making this assertion
            confidence: Optional confidence (0.0-1.0)
            source: Optional source of information
            rationale: Optional why this assertion was made

        Returns:
            Created assertion
        """
        assertion = Assertion(
            id=self.id_provider.next(),
            namespace=self.namespace,
            subject=subject,
            predicate=predicate,
            value_kind="literal",
            value_type=value_type,
            value=value,
            author=author,
            confidence=confidence,
            source=source,
            rationale=rationale,
            asserted_at=self.clock.now(),
        )

        self.backend.put_assertion(assertion)
        return assertion

    def assert_ref(
        self,
        subject: str,
        predicate: str,
        target: str,
        author: str,
        *,
        confidence: float | None = None,
        source: str | None = None,
    ) -> Assertion:
        """Make a reference assertion (relation) between entities.

        Args:
            subject: Source entity ID
            predicate: Relation name (e.g., "Person.employer")
            target: Target entity ID
            author: Principal ID making this assertion
            confidence: Optional confidence (0.0-1.0)
            source: Optional source of information

        Returns:
            Created assertion
        """
        assertion = Assertion(
            id=self.id_provider.next(),
            namespace=self.namespace,
            subject=subject,
            predicate=predicate,
            value_kind="ref",
            value=target,
            author=author,
            confidence=confidence,
            source=source,
            asserted_at=self.clock.now(),
        )

        self.backend.put_assertion(assertion)
        return assertion

    def get_entity(self, entity_id: str) -> Entity | None:
        """Retrieve an entity by ID.

        Args:
            entity_id: Entity ID

        Returns:
            Entity if found, None otherwise
        """
        return self.backend.get_entity(entity_id)

    def assertions(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        status: str | None = "active",
    ) -> list[Assertion]:
        """Query assertions.

        Args:
            subject: Filter by subject entity ID
            predicate: Filter by predicate
            status: Filter by status (default: active only)

        Returns:
            List of matching assertions
        """
        return self.backend.assertions(
            subject=subject, predicate=predicate, status=status
        )

    def close(self) -> None:
        """Close the knowledge base connection."""
        self.backend.close()


__all__ = ["Ontology"]
