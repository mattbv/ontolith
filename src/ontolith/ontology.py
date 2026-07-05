"""Ontology - main entry point for the knowledge base.

The Ontology class is the primary API surface for users. It wraps the storage
backend and provides high-level methods for entities, assertions, and queries.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ontolith.core import (
    Assertion,
    Clock,
    Entity,
    IdProvider,
    SystemClock,
    UlidProvider,
)
from ontolith.core.errors import (
    AuthError,
    CapabilityError,
    NotFoundError,
    SchemaError,
    ValidationError,
)
from ontolith.govern import AutoAccept, ThresholdPolicy
from ontolith.govern.conflict import ConflictResult, Contradict, Supersede, route
from ontolith.govern.contradiction import Contradiction
from ontolith.govern.policy import Decision, Reject
from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal
from ontolith.query import QueryBuilder
from ontolith.schema import SchemaIR
from ontolith.store.base import StorageBackend


class AsOfView:
    """Read-only bitemporal view at a specific point in time (SPEC §10).

    Reconstructs what was known and true at time `t`:
        valid_from <= t < (valid_to or ∞)  AND  asserted_at <= t

    Status is NOT used as a filter — the temporal dimensions determine visibility.
    """

    def __init__(
        self,
        backend: "StorageBackend",
        as_of: datetime,
        namespace: str,
    ) -> None:
        self._backend = backend
        self._as_of = as_of
        self._namespace = namespace

    def assertions(
        self,
        subject: str | None = None,
        predicate: str | None = None,
    ) -> list[Assertion]:
        """Assertions visible at the as_of timestamp."""
        return self._backend.assertions(
            subject=subject,
            predicate=predicate,
            status=None,
            as_of_time=self._as_of,
        )

    def query(self, concept: str) -> QueryBuilder:
        """Query entities as they existed at the as_of timestamp."""
        return QueryBuilder(
            backend=self._backend,
            namespace=self._namespace,
            concept=concept,
            as_of_time=self._as_of,
        )


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

        effective_clock = clock or SystemClock()
        backend = SQLiteBackend(path, clock=effective_clock)
        return cls(backend, clock=effective_clock, id_provider=id_provider)

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
        return self.backend.assertions(subject=subject, predicate=predicate, status=status)

    def query(self, concept: str) -> QueryBuilder:
        """Create a query builder for a concept.

        Args:
            concept: Concept name to query

        Returns:
            QueryBuilder for fluent filtering

        Example:
            >>> kb.query("Person").where(name="Ada Lovelace").all()
        """
        return QueryBuilder(
            backend=self.backend,
            namespace=self.namespace,
            concept=concept,
        )

    def as_of(self, t: datetime | str) -> AsOfView:
        """Return a read-only bitemporal view at time t (SPEC §10).

        Reconstructs what was known and true at t:
            valid_from <= t < (valid_to or ∞)  AND  asserted_at <= t

        Args:
            t: Point in time — datetime or ISO-format string

        Returns:
            AsOfView for querying the knowledge base as it stood at t
        """
        if isinstance(t, str):
            t = datetime.fromisoformat(t)
        return AsOfView(self.backend, t, self.namespace)

    def propose(
        self,
        subject: str,
        predicate: str,
        value: str,
        value_type: str,
        author: str,
        *,
        temporality: Literal["static", "time_varying"] = "static",
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
        acting_as: str | None = None,
    ) -> tuple[Proposal, Decision]:
        """Submit a literal assertion through the proposal/policy path (SPEC §9).

        Evaluates ThresholdPolicy. Auto-accepted proposals are committed
        immediately with SPEC §10 conflict routing; others are stored for review.

        When ``acting_as`` is set the proposal is made on behalf of another
        principal (delegation, ADR-0003). Policy is evaluated using the
        delegating principal's capability and trust level.

        Returns:
            (Proposal, Decision) tuple
        """
        principal = self.backend.get_principal(author)
        if principal is None:
            raise AuthError(f"Principal not found: {author}")

        delegating: Principal | None = None
        if acting_as is not None and acting_as != author:
            delegating = self.backend.get_principal(acting_as)
            if delegating is None:
                raise AuthError(f"Delegating principal not found: {acting_as}")
            # Authorization: author must be owned by acting_as (ADR-0003)
            if principal.owner != acting_as:
                raise CapabilityError(
                    f"Principal {author!r} is not authorized to act as {acting_as!r}"
                )

        now = self.clock.now()
        proposal_id = self.id_provider.next()
        proposal = Proposal(
            id=proposal_id,
            namespace=self.namespace,
            author=author,
            acting_as=acting_as,
            state="submitted",
            created_at=now,
            payload={
                "operations": [
                    {
                        "kind": "assert_literal",
                        "subject": subject,
                        "predicate": predicate,
                        "value": value,
                        "value_type": value_type,
                        "temporality": temporality,
                        "confidence": confidence,
                        "source": source,
                        "rationale": rationale,
                    }
                ]
            },
        )

        # Policy uses delegating principal when acting_as is set (ADR-0003)
        effective_principal = delegating if delegating is not None else principal
        decision = ThresholdPolicy().evaluate(proposal, effective_principal)

        if isinstance(decision, AutoAccept):
            assertion = Assertion(
                id=self.id_provider.next(),
                namespace=self.namespace,
                subject=subject,
                predicate=predicate,
                value_kind="literal",
                value_type=value_type,
                value=value,
                author=author,
                acting_as=acting_as,
                confidence=confidence,
                source=source,
                rationale=rationale,
                asserted_at=now,
                proposal_id=proposal_id,
            )
            accepted = proposal.model_copy(
                update={
                    "state": "auto_accepted",
                    "decided_at": now,
                    "policy_reason": decision.reason,
                }
            )
            with self.backend.transaction():
                self.backend.put_proposal(accepted)
                self._apply_with_conflict_routing(assertion, temporality)
            return accepted, decision

        if isinstance(decision, Reject):
            rejected = proposal.model_copy(
                update={
                    "state": "rejected",
                    "decided_at": now,
                    "policy_reason": decision.reason,
                }
            )
            self.backend.put_proposal(rejected)
            return rejected, decision

        pending = proposal.model_copy(
            update={
                "state": "require_review",
                "policy_reason": getattr(decision, "reason", None),
            }
        )
        self.backend.put_proposal(pending)
        return pending, decision

    def retract(
        self,
        assertion_id: str,
        author: str,
        *,
        acting_as: str | None = None,
    ) -> tuple[Proposal, Decision]:
        """Propose retraction of an assertion through the policy path (SPEC §9).

        When ``acting_as`` is set the retraction is made on behalf of another
        principal (delegation, ADR-0003).

        Returns:
            (Proposal, Decision) tuple
        """
        principal = self.backend.get_principal(author)
        if principal is None:
            raise AuthError(f"Principal not found: {author}")

        delegating: Principal | None = None
        if acting_as is not None and acting_as != author:
            delegating = self.backend.get_principal(acting_as)
            if delegating is None:
                raise AuthError(f"Delegating principal not found: {acting_as}")
            if principal.owner != acting_as:
                raise CapabilityError(
                    f"Principal {author!r} is not authorized to act as {acting_as!r}"
                )

        now = self.clock.now()
        proposal_id = self.id_provider.next()
        proposal = Proposal(
            id=proposal_id,
            namespace=self.namespace,
            author=author,
            acting_as=acting_as,
            state="submitted",
            created_at=now,
            payload={"operations": [{"kind": "retract", "assertion_id": assertion_id}]},
        )

        effective_principal = delegating if delegating is not None else principal
        decision = ThresholdPolicy().evaluate(proposal, effective_principal)

        if isinstance(decision, AutoAccept):
            accepted = proposal.model_copy(
                update={
                    "state": "auto_accepted",
                    "decided_at": now,
                    "policy_reason": decision.reason,
                }
            )
            with self.backend.transaction():
                self.backend.put_proposal(accepted)
                self.backend.set_assertion_status(assertion_id, "retracted")
            return accepted, decision

        if isinstance(decision, Reject):
            rejected = proposal.model_copy(
                update={
                    "state": "rejected",
                    "decided_at": now,
                    "policy_reason": decision.reason,
                }
            )
            self.backend.put_proposal(rejected)
            return rejected, decision

        pending = proposal.model_copy(
            update={
                "state": "require_review",
                "policy_reason": getattr(decision, "reason", None),
            }
        )
        self.backend.put_proposal(pending)
        return pending, decision

    def _apply_with_conflict_routing(
        self,
        assertion: Assertion,
        temporality: Literal["static", "time_varying"],
    ) -> None:
        """Apply an assertion with SPEC §10 conflict routing. Must run inside a transaction."""
        open_contradiction = self.backend.get_open_contradiction(
            self.namespace, assertion.subject, assertion.predicate
        )

        # If a contradiction is already open, all existing assertions for this
        # (subject, predicate) are flagged — no active ones exist. Any new
        # incoming assertion must be added to the same contradiction.
        if open_contradiction is not None and temporality == "static":
            all_member_ids = list(dict.fromkeys(open_contradiction.member_ids + [assertion.id]))
            result: ConflictResult = Contradict(
                member_ids=all_member_ids,
                existing_contradiction_id=open_contradiction.id,
            )
        else:
            existing = self.backend.assertions(
                subject=assertion.subject,
                predicate=assertion.predicate,
                status="active",
            )
            result = route(
                incoming=assertion,
                existing=existing,
                temporality=temporality,
                existing_contradiction_id=open_contradiction.id if open_contradiction else None,
            )

        if isinstance(result, Supersede):
            # Close prior window at the incoming assertion's valid_from (SPEC §10.2).
            # valid_from is guaranteed non-None after Assertion validation.
            close_at = (assertion.valid_from or assertion.asserted_at).isoformat()
            for target_id in result.targets:
                self.backend.set_assertion_status(target_id, "superseded", valid_to=close_at)
            supersedes_id = result.targets[0] if result.targets else None
            final = assertion.model_copy(update={"supersedes": supersedes_id})
            self.backend.put_assertion(final)

        elif isinstance(result, Contradict):
            for mid in result.member_ids:
                if mid != assertion.id:
                    self.backend.set_assertion_status(mid, "flagged")
            flagged = assertion.model_copy(update={"status": "flagged"})
            self.backend.put_assertion(flagged)
            if result.existing_contradiction_id and open_contradiction:
                merged = list(dict.fromkeys(open_contradiction.member_ids + result.member_ids))
                self.backend.update_contradiction_members(result.existing_contradiction_id, merged)
            else:
                contradiction = Contradiction(
                    id=self.id_provider.next(),
                    namespace=self.namespace,
                    subject=assertion.subject,
                    predicate=assertion.predicate,
                    state="open",
                    member_ids=result.member_ids,
                    created_at=assertion.asserted_at,
                )
                self.backend.put_contradiction(contradiction)

        else:
            self.backend.put_assertion(assertion)

    def accept_proposal(self, proposal_id: str, reviewer: str) -> Proposal:
        """Accept a pending proposal, replaying its operations (SPEC §9).

        The reviewer must have `review` or `admin` capability.
        Only proposals in `require_review` or `under_review` state can be accepted.
        Operations are replayed through SPEC §10 conflict routing inside a single transaction.

        Args:
            proposal_id: ID of the proposal to accept
            reviewer: Principal ID of the reviewer

        Returns:
            Updated Proposal with state `accepted`
        """
        reviewer_principal = self.backend.get_principal(reviewer)
        if reviewer_principal is None:
            raise AuthError(f"Principal not found: {reviewer}")
        if reviewer_principal.default_capability not in ("review", "admin"):
            raise CapabilityError(f"Principal {reviewer} lacks review capability")

        proposal = self.backend.get_proposal(proposal_id)
        if proposal is None:
            raise NotFoundError(f"Proposal not found: {proposal_id}")
        if proposal.state not in ("require_review", "under_review"):
            raise ValidationError(
                f"Proposal {proposal_id} is not pending review (state: {proposal.state})"
            )

        now = self.clock.now()

        with self.backend.transaction():
            for op in proposal.payload.get("operations", []):
                if op["kind"] == "assert_literal":
                    assertion = Assertion(
                        id=self.id_provider.next(),
                        namespace=self.namespace,
                        subject=op["subject"],
                        predicate=op["predicate"],
                        value_kind="literal",
                        value_type=op["value_type"],
                        value=op["value"],
                        author=proposal.author,
                        confidence=op.get("confidence"),
                        source=op.get("source"),
                        rationale=op.get("rationale"),
                        asserted_at=now,
                        proposal_id=proposal_id,
                    )
                    self._apply_with_conflict_routing(assertion, op.get("temporality", "static"))
                elif op["kind"] == "retract":
                    self.backend.set_assertion_status(op["assertion_id"], "retracted")
                else:
                    raise ValidationError(
                        f"Unknown operation kind in proposal payload: {op['kind']}"
                    )

            self.backend.update_proposal_state(
                proposal_id, "accepted", now.isoformat(), f"Accepted by reviewer {reviewer}"
            )

        accepted = self.backend.get_proposal(proposal_id)
        assert accepted is not None
        return accepted

    def reject_proposal(self, proposal_id: str, reviewer: str, reason: str = "") -> Proposal:
        """Reject a pending proposal (SPEC §9).

        The reviewer must have `review` or `admin` capability.
        No operations are applied; the proposal is marked rejected.

        Args:
            proposal_id: ID of the proposal to reject
            reviewer: Principal ID of the reviewer
            reason: Optional rejection reason

        Returns:
            Updated Proposal with state `rejected`
        """
        reviewer_principal = self.backend.get_principal(reviewer)
        if reviewer_principal is None:
            raise AuthError(f"Principal not found: {reviewer}")
        if reviewer_principal.default_capability not in ("review", "admin"):
            raise CapabilityError(f"Principal {reviewer} lacks review capability")

        proposal = self.backend.get_proposal(proposal_id)
        if proposal is None:
            raise NotFoundError(f"Proposal not found: {proposal_id}")
        if proposal.state not in ("require_review", "under_review"):
            raise ValidationError(
                f"Proposal {proposal_id} is not pending review (state: {proposal.state})"
            )

        now = self.clock.now()
        self.backend.update_proposal_state(
            proposal_id,
            "rejected",
            now.isoformat(),
            reason or f"Rejected by reviewer {reviewer}",
        )

        rejected = self.backend.get_proposal(proposal_id)
        assert rejected is not None
        return rejected

    def resolve_contradiction(
        self,
        contradiction_id: str,
        winner_assertion_id: str,
        resolver: str,
    ) -> Contradiction:
        """Resolve an open contradiction by selecting a winning assertion (SPEC §10.3).

        All other member assertions are retracted; the winning assertion is
        reactivated. The resolver must have `review` or `admin` capability.
        Resolution is recorded on the contradiction and appears in provenance.

        Args:
            contradiction_id: ID of the contradiction to resolve
            winner_assertion_id: ID of the member assertion to keep active
            resolver: Principal ID resolving the contradiction

        Returns:
            Updated Contradiction with state `resolved`
        """
        resolver_principal = self.backend.get_principal(resolver)
        if resolver_principal is None:
            raise AuthError(f"Principal not found: {resolver}")
        if resolver_principal.default_capability not in ("review", "admin"):
            raise CapabilityError(f"Principal {resolver} lacks review capability")

        contradiction = self.backend.get_contradiction(contradiction_id)
        if contradiction is None:
            raise NotFoundError(f"Contradiction not found: {contradiction_id}")
        if contradiction.state != "open":
            raise ValidationError(
                f"Contradiction {contradiction_id} is not open (state: {contradiction.state})"
            )
        if winner_assertion_id not in contradiction.member_ids:
            raise ValidationError(
                f"Assertion {winner_assertion_id} is not a member of "
                f"contradiction {contradiction_id}"
            )

        now = self.clock.now()
        with self.backend.transaction():
            for member_id in contradiction.member_ids:
                if member_id != winner_assertion_id:
                    self.backend.set_assertion_status(member_id, "retracted")
            self.backend.set_assertion_status(winner_assertion_id, "active")
            self.backend.resolve_contradiction(contradiction_id, resolver, now)

        resolved = self.backend.get_contradiction(contradiction_id)
        assert resolved is not None
        return resolved

    def apply_schema(self, schema: SchemaIR, author: str) -> SchemaIR:
        """Persist a new schema version, capability-checked (SPEC §6).

        The author must have `admin` capability. Versions must be applied in
        strict monotonic order: 1 if no schema exists yet for the namespace,
        otherwise exactly `current_latest + 1`.

        This is the only governed path that reaches `StorageBackend.put_schema()` —
        without it, schema versions could be persisted with no capability check
        by anything holding a `backend` reference directly.

        Args:
            schema: SchemaIR to persist
            author: Principal ID applying the schema

        Returns:
            The persisted SchemaIR

        Raises:
            AuthError: If the author principal is not found
            CapabilityError: If the author lacks `admin` capability
            SchemaError: If `schema.version` is not the next monotonic version
        """
        author_principal = self.backend.get_principal(author)
        if author_principal is None:
            raise AuthError(f"Principal not found: {author}")
        if author_principal.default_capability != "admin":
            raise CapabilityError(f"Principal {author} lacks admin capability")

        current = self.backend.get_schema(schema.namespace)
        expected_version = (current.version + 1) if current is not None else 1
        if schema.version != expected_version:
            raise SchemaError(
                f"Schema version {schema.version} is not the next monotonic version "
                f"for namespace {schema.namespace!r} (expected {expected_version})"
            )

        self.backend.put_schema(schema)
        return schema

    def close(self) -> None:
        """Close the knowledge base connection."""
        self.backend.close()


__all__ = ["AsOfView", "Ontology"]
