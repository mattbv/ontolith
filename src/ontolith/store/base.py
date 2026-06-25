"""Abstract storage backend port.

The StorageBackend port defines the interface that all concrete storage
adapters (SQLite, DuckDB, graph engines, etc.) must implement.
"""

from typing import Protocol

from ontolith.core import Assertion, Entity


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

    def close(self) -> None:
        """Close the storage backend and release resources."""
        ...


__all__ = ["StorageBackend"]
