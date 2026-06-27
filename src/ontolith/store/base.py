"""Abstract storage backend port.

The StorageBackend port defines the interface that all concrete storage
adapters (SQLite, DuckDB, graph engines, etc.) must implement.

Note: This port imports domain models (Entity, Assertion) which creates
a bidirectional dependency between store.base and core. This is acceptable
because StorageBackend is an abstract protocol, not a concrete implementation.
The dependency rule prevents domain logic from importing concrete adapters.
"""

from contextlib import AbstractContextManager
from typing import Protocol

from ontolith.core import Assertion, Entity
from ontolith.identity import Principal
from ontolith.schema import SchemaIR


class StorageBackend(Protocol):
    """Abstract port for storage adapters.

    Concrete backends implement this protocol to provide:
    - Transaction management
    - Entity and assertion persistence
    - Query execution
    - Vector search (for hybrid retrieval)

    The default implementation (M1) is SQLite + sqlite-vec.
    Alternative backends can be plugged in via this interface.
    """

    def begin(self) -> None:
        """Begin a new transaction."""
        ...

    def commit(self) -> None:
        """Commit the current transaction."""
        ...

    def rollback(self) -> None:
        """Rollback the current transaction."""
        ...

    def transaction(self) -> AbstractContextManager[None]:
        """Context manager for atomic multi-write transactions.

        Guarantees rollback on any exception. Prefer this over
        manual begin/commit/rollback to avoid wedged connections.
        """
        ...

    def put_principal(self, principal: Principal) -> None:
        """Persist a principal.

        Args:
            principal: Principal to persist

        Raises:
            StorageError: If persistence fails
        """
        ...

    def get_principal(self, principal_id: str) -> Principal | None:
        """Retrieve a principal by ID.

        Args:
            principal_id: Principal ID to retrieve

        Returns:
            Principal if found, None otherwise
        """
        ...

    def put_entity(self, entity: Entity) -> None:
        """Persist an entity.

        Args:
            entity: Entity to persist

        Raises:
            StorageError: If persistence fails
        """
        ...

    def put_assertion(self, assertion: Assertion) -> None:
        """Persist an assertion.

        Args:
            assertion: Assertion to persist

        Raises:
            StorageError: If persistence fails
        """
        ...

    def get_entity(self, entity_id: str) -> Entity | None:
        """Retrieve an entity by ID.

        Args:
            entity_id: Entity ID to retrieve

        Returns:
            Entity if found, None otherwise
        """
        ...

    def assertions(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        status: str | None = None,
    ) -> list[Assertion]:
        """Query assertions with optional filters.

        Args:
            subject: Filter by subject entity ID
            predicate: Filter by predicate
            status: Filter by status (default: active only)

        Returns:
            List of matching assertions
        """
        ...

    def set_assertion_status(
        self,
        assertion_id: str,
        status: str,
        valid_to: str | None = None,
    ) -> None:
        """Update assertion status and optionally close validity window.

        This is the ONLY allowed mutation on assertions (append-only invariant).
        Used for supersession and retraction.

        Args:
            assertion_id: Assertion ID to update
            status: New status (superseded, retracted, flagged)
            valid_to: Optional validity end time (ISO format)

        Raises:
            StorageError: If update fails or assertion not found
        """
        ...

    def put_schema(self, schema: SchemaIR) -> None:
        """Persist a schema version.

        Args:
            schema: Schema to persist

        Raises:
            StorageError: If persistence fails

        Note:
            Schema versions are never deleted (required for time-travel).
        """
        ...

    def get_schema(self, namespace: str, version: int | None = None) -> SchemaIR | None:
        """Retrieve a schema version.

        Args:
            namespace: Namespace to query
            version: Specific version, or None for latest

        Returns:
            Schema if found, None otherwise
        """
        ...

    def entities(
        self,
        namespace: str | None = None,
        concept: str | None = None,
    ) -> list[Entity]:
        """Query entities with optional filters.

        Args:
            namespace: Filter by namespace
            concept: Filter by concept

        Returns:
            List of matching entities
        """
        ...

    def close(self) -> None:
        """Close the storage backend and release resources."""
        ...


__all__ = ["StorageBackend"]
