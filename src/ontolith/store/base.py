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

from ontolith.core import Assertion, AssertionEvent, Entity
from ontolith.govern.contradiction import Contradiction
from ontolith.govern.proposal import Proposal, ProposalEvent
from ontolith.identity import Principal, PrincipalCredential
from ontolith.schema import SchemaIR

VECTOR_SCOPES = frozenset({"entity", "assertion"})
"""Closed set of embedding scopes (SPEC §11.3): entity- and assertion-level.

Deliberately not an arbitrary caller-supplied string — both backends use
`scope` to name per-scope storage (SQLite: a vec0 virtual table per scope;
DuckDB: a plain table per scope), so an open string would mean dynamic DDL
driven by caller input. vector_upsert/vector_search MUST reject any scope
outside this set with ValidationError.
"""


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

    def put_credential(self, credential: PrincipalCredential) -> None:
        """Persist a principal credential (hashed API-key token, ADR-0014).

        Args:
            credential: PrincipalCredential to persist

        Raises:
            StorageError: If persistence fails
        """
        ...

    def get_principal_by_token_hash(self, token_hash: str) -> Principal | None:
        """Resolve a principal via a credential's token hash.

        Only unrevoked credentials resolve.

        Args:
            token_hash: SHA-256 hash of the raw bearer token

        Returns:
            Principal if the hash matches an active credential, None otherwise
        """
        ...

    def get_credential(self, credential_id: str) -> PrincipalCredential | None:
        """Retrieve a credential by ID.

        Args:
            credential_id: Credential ID to retrieve

        Returns:
            PrincipalCredential if found, None otherwise
        """
        ...

    def get_credentials_for_principal(self, principal_id: str) -> list[PrincipalCredential]:
        """List all credentials (active and revoked) issued to a principal.

        Never exposes the raw token — only credential metadata (id,
        created_at, revoked_at). Used to discover a credential ID to revoke.

        Args:
            principal_id: Principal to list credentials for

        Returns:
            Credentials for this principal, most recently issued first
        """
        ...

    def revoke_credential(self, credential_id: str, revoked_at: datetime) -> None:
        """Mark a credential as revoked.

        Args:
            credential_id: Credential to revoke
            revoked_at: Timestamp of revocation

        Raises:
            StorageError: If the credential is not found
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

    def get_assertion(self, assertion_id: str) -> Assertion | None:
        """Retrieve a single assertion by ID, regardless of status.

        Args:
            assertion_id: Assertion ID to retrieve

        Returns:
            Assertion if found, None otherwise
        """
        ...

    def assertions(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        status: str | None = "active",
        as_of_time: datetime | None = None,
        include_flagged: bool = False,
    ) -> list[Assertion]:
        """Query assertions with optional filters.

        Args:
            subject: Filter by subject entity ID
            predicate: Filter by predicate
            status: Filter by current status (ignored when as_of_time is set).
                Defaults to "active"; pass status=None for every status.
            as_of_time: If set, applies bitemporal filter:
                valid_from <= t < (valid_to or ∞) AND asserted_at <= t
            include_flagged: When as_of_time is set, whether to include
                'flagged' assertions (excluded by default)

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

    def put_assertion_event(self, event: AssertionEvent) -> None:
        """Persist an append-only assertion status-mutation event.

        Args:
            event: AssertionEvent to persist

        Raises:
            StorageError: If persistence fails
        """
        ...

    def get_assertion_events(self, assertion_id: str) -> list[AssertionEvent]:
        """Retrieve all status-mutation events for an assertion, oldest first.

        Args:
            assertion_id: Assertion to retrieve events for

        Returns:
            Events for this assertion, ordered by occurrence
        """
        ...

    def get_assertion_events_by_successor(self, successor_id: str) -> list[AssertionEvent]:
        """Retrieve all 'superseded' events caused by a given successor assertion.

        Recovers the full predecessor set for a supersession (KI-008):
        Assertion.supersedes only records the first predecessor when one
        incoming assertion supersedes several concurrently-overlapping ones,
        but every superseded predecessor gets its own event row here.

        Args:
            successor_id: Assertion ID that caused the supersession(s)

        Returns:
            Events with this successor_id, ordered by occurrence. Each
            event's assertion_id is one predecessor that was superseded.
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
        include_flagged: bool = False,
    ) -> list[Entity]:
        """Query entities matching all predicate=value filters in one SQL query.

        Avoids the N+1 pattern of entities() + per-entity assertions() calls.
        Each filter is (full_predicate, literal_value); ALL must match (AND semantics).

        Args:
            namespace: Namespace to query
            concept: Concept to filter by
            predicate_filters: Dict of full_predicate → literal_value
            as_of_time: If set, applies bitemporal filter on assertions and entity creation
            include_flagged: When as_of_time is set, whether to include
                'flagged' assertions in the predicate match (excluded by default)

        Returns:
            List of entities where all filters match at the given time
        """
        ...

    def vector_upsert(self, scope: str, id: str, vec: list[float]) -> None:
        """Insert or replace the embedding vector for (scope, id).

        The dimensionality of the first vector ever upserted into a scope
        establishes that scope's dimension for the life of the store; later
        upserts into the same scope must match it.

        Args:
            scope: Embedding scope. Must be one of VECTOR_SCOPES.
            id: Entity or assertion ID the vector represents.
            vec: Embedding vector.

        Raises:
            ValidationError: scope is not in VECTOR_SCOPES, or vec's length
                does not match the scope's already-established dimension.
            StorageError: If persistence fails.
        """
        ...

    def vector_search(self, scope: str, vec: list[float], k: int) -> list[tuple[str, float]]:
        """Return the k nearest ids to vec within scope, ascending distance.

        Distance is L2 (Euclidean). Embedder implementations MUST return
        L2-unit-normalized vectors, which makes ascending-L2-distance order
        equivalent to descending-cosine-similarity order.

        Args:
            scope: Embedding scope. Must be one of VECTOR_SCOPES.
            vec: Query vector.
            k: Maximum number of results.

        Returns:
            (id, distance) tuples, nearest first. Fewer than k if the scope
            has fewer than k vectors; empty list if the scope has never
            been populated.

        Raises:
            ValidationError: scope is not in VECTOR_SCOPES, or vec's length
                does not match the scope's already-established dimension.
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

    def proposals(self, state: str | None = None) -> list[Proposal]:
        """Query proposals, optionally filtered by state (SPEC §14.1).

        Args:
            state: Filter by proposal state (e.g. "require_review");
                None returns proposals in every state

        Returns:
            Matching proposals, most recently created first
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
            policy_reason: Human-readable reason from policy engine. If None,
                the stored value is left unchanged (not cleared) — the
                policy-engine reason set at proposal-creation time is
                distinct from, and not overwritten by, review actions
                recorded via put_proposal_event.
        """
        ...

    def put_proposal_event(self, event: ProposalEvent) -> None:
        """Persist a structured review-action event (SPEC §9.4).

        Args:
            event: ProposalEvent to persist

        Raises:
            StorageError: If persistence fails
        """
        ...

    def get_proposal_events(self, proposal_id: str) -> list[ProposalEvent]:
        """Retrieve all review events for a proposal, oldest first.

        Args:
            proposal_id: Proposal to retrieve events for

        Returns:
            Events for this proposal, ordered by occurrence
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

    def contradictions(self, state: str | None = None) -> list[Contradiction]:
        """Query contradictions, optionally filtered by state (SPEC §14.1).

        Args:
            state: Filter by contradiction state ("open" or "resolved");
                None returns contradictions in every state

        Returns:
            Matching contradictions, most recently created first
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
