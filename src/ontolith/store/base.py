"""Abstract storage backend port.

The StorageBackend port defines the interface that all concrete storage
adapters (SQLite, DuckDB, graph engines, etc.) must implement.

Note: This port imports domain models (Entity, Assertion) which creates
a bidirectional dependency between store.base and core. This is acceptable
because StorageBackend is an abstract protocol, not a concrete implementation.
The dependency rule prevents domain logic from importing concrete adapters.
"""

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from ontolith.core import Assertion, Entity
from ontolith.govern.contradiction import Contradiction
from ontolith.govern.proposal import Proposal
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
        as_of_time: datetime | None = None,
    ) -> list[Assertion]:
        """Query assertions with optional filters.

        Args:
            subject: Filter by subject entity ID
            predicate: Filter by predicate
            status: Filter by current status (ignored when as_of_time is set)
            as_of_time: If set, applies bitemporal filter:
                valid_from <= t < (valid_to or ∞) AND asserted_at <= t

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
        as_of_time: datetime | None = None,
    ) -> list[Entity]:
        """Query entities with optional filters.

        Args:
            namespace: Filter by namespace
            concept: Filter by concept
            as_of_time: If set, exclude entities created after this time

        Returns:
            List of matching entities
        """
        ...

    def entities_where(
        self,
        namespace: str,
        concept: str,
        predicate_filters: dict[str, str],
        as_of_time: datetime | None = None,
    ) -> list[Entity]:
        """Query entities matching all predicate=value filters in one SQL query.

        Avoids the N+1 pattern of entities() + per-entity assertions() calls.
        Each filter is (full_predicate, literal_value); ALL must match (AND semantics).

        Args:
            namespace: Namespace to query
            concept: Concept to filter by
            predicate_filters: Dict of full_predicate → literal_value
            as_of_time: If set, applies bitemporal filter on assertions and entity creation

        Returns:
            List of entities where all filters match at the given time
        """
        ...

    def put_proposal(self, proposal: Proposal) -> None:
        """Persist a proposal.

        Args:
            proposal: Proposal to persist

        Raises:
            StorageError: If persistence fails
        """
        ...

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        """Retrieve a proposal by ID.

        Args:
            proposal_id: Proposal ID

        Returns:
            Proposal if found, None otherwise
        """
        ...

    def update_proposal_state(
        self,
        proposal_id: str,
        state: str,
        decided_at: str | None = None,
        policy_reason: str | None = None,
    ) -> None:
        """Update proposal state after policy decision.

        Args:
            proposal_id: Proposal to update
            state: New state (auto_accepted, require_review, rejected, etc.)
            decided_at: ISO timestamp of the decision
            policy_reason: Human-readable reason from policy engine
        """
        ...

    def put_contradiction(self, contradiction: Contradiction) -> None:
        """Persist a new contradiction.

        Args:
            contradiction: Contradiction to persist

        Raises:
            StorageError: If persistence fails
        """
        ...

    def get_open_contradiction(
        self,
        namespace: str,
        subject: str,
        predicate: str,
    ) -> Contradiction | None:
        """Return the open contradiction for (namespace, subject, predicate), if any.

        Args:
            namespace: Namespace to search
            subject: Subject entity ID
            predicate: Predicate name

        Returns:
            Open Contradiction if one exists, None otherwise
        """
        ...

    def update_contradiction_members(
        self,
        contradiction_id: str,
        member_ids: list[str],
    ) -> None:
        """Replace the member_ids list on an existing contradiction.

        Args:
            contradiction_id: Contradiction to update
            member_ids: New full list of member assertion IDs
        """
        ...

    def get_contradiction(self, contradiction_id: str) -> Contradiction | None:
        """Retrieve a contradiction by ID, regardless of state.

        Args:
            contradiction_id: Contradiction ID to retrieve

        Returns:
            Contradiction if found, None otherwise
        """
        ...

    def resolve_contradiction(
        self,
        contradiction_id: str,
        resolved_by: str,
        resolved_at: datetime,
    ) -> None:
        """Mark a contradiction as resolved (SPEC §10.3).

        Args:
            contradiction_id: Contradiction to resolve
            resolved_by: Principal ID who resolved it
            resolved_at: Timestamp of resolution

        Raises:
            StorageError: If the contradiction is not found or update fails
        """
        ...

    def close(self) -> None:
        """Close the storage backend and release resources."""
        ...


__all__ = ["StorageBackend"]
