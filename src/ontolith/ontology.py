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
from ontolith.identity import Principal, PrincipalCredential, min_capability
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

    def _check_direct_write_capability(
        self, author: str, acting_as: str | None
    ) -> tuple[Principal, Principal | None]:
        """Shared auth/capability gate for assert_literal/assert_ref (SPEC §9.3).

        A principal with `write` (or `admin`) capability MAY bypass proposals,
        but direct writes still pass through conflict routing (§10) and
        provenance is still recorded. AI-kind principals are never permitted
        this path, even if misconfigured with elevated capability — AI
        proposals always require review (ADR-0003); direct writes skip review
        entirely.

        Returns:
            (author principal, delegating principal or None)

        Raises:
            AuthError: author or acting_as is not a known principal
            CapabilityError: author is AI-kind, delegation is unauthorized, or
                effective capability is below `write`
        """
        principal = self.backend.get_principal(author)
        if principal is None:
            raise AuthError(f"Principal not found: {author}")
        if principal.kind == "ai":
            raise CapabilityError(f"AI principal {author!r} cannot make direct writes")

        delegating: Principal | None = None
        if acting_as is not None and acting_as != author:
            delegating = self.backend.get_principal(acting_as)
            if delegating is None:
                raise AuthError(f"Delegating principal not found: {acting_as}")
            if principal.owner != acting_as:
                raise CapabilityError(
                    f"Principal {author!r} is not authorized to act as {acting_as!r}"
                )

        # SPEC §8.4: effective capability is min(author, acting_as) when
        # delegating, not a wholesale substitution.
        capability: str = principal.default_capability
        if delegating is not None:
            capability = min_capability(capability, delegating.default_capability)
        if capability not in ("write", "admin"):
            raise CapabilityError(f"Principal {author!r} lacks write capability")

        return principal, delegating

    def _resolve_temporality(self, predicate: str) -> Literal["static", "time_varying"]:
        schema = self.backend.get_schema(self.namespace)
        return schema.temporality_of(predicate) if schema is not None else "static"

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
        acting_as: str | None = None,
    ) -> Assertion:
        """Make a literal assertion about an entity, bypassing the proposal queue.

        SPEC §9.3: requires `write` capability (or `admin`); still passes
        through SPEC §10 conflict routing and records full provenance.

        Args:
            subject: Entity ID
            predicate: Property name (e.g., "Person.name")
            value: Literal value
            value_type: Type of literal (Text, Integer, etc.)
            author: Principal ID making this assertion
            confidence: Optional confidence (0.0-1.0)
            source: Optional source of information
            rationale: Optional why this assertion was made
            acting_as: Optional principal ID being acted on behalf of (delegation)

        Returns:
            Assertion as persisted (status/supersedes reflect conflict routing)
        """
        self._check_direct_write_capability(author, acting_as)
        temporality = self._resolve_temporality(predicate)

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
            asserted_at=self.clock.now(),
        )

        with self.backend.transaction():
            return self._apply_with_conflict_routing(assertion, temporality)

    def assert_ref(
        self,
        subject: str,
        predicate: str,
        target: str,
        author: str,
        *,
        confidence: float | None = None,
        source: str | None = None,
        acting_as: str | None = None,
    ) -> Assertion:
        """Make a reference assertion (relation) between entities, bypassing the
        proposal queue.

        SPEC §9.3: requires `write` capability (or `admin`); still passes
        through SPEC §10 conflict routing and records full provenance.

        Args:
            subject: Source entity ID
            predicate: Relation name (e.g., "Person.employer")
            target: Target entity ID
            author: Principal ID making this assertion
            confidence: Optional confidence (0.0-1.0)
            source: Optional source of information
            acting_as: Optional principal ID being acted on behalf of (delegation)

        Returns:
            Assertion as persisted (status/supersedes reflect conflict routing)
        """
        self._check_direct_write_capability(author, acting_as)
        temporality = self._resolve_temporality(predicate)

        assertion = Assertion(
            id=self.id_provider.next(),
            namespace=self.namespace,
            subject=subject,
            predicate=predicate,
            value_kind="ref",
            value=target,
            author=author,
            acting_as=acting_as,
            confidence=confidence,
            source=source,
            asserted_at=self.clock.now(),
        )

        with self.backend.transaction():
            return self._apply_with_conflict_routing(assertion, temporality)

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
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
        acting_as: str | None = None,
    ) -> tuple[Proposal, Decision]:
        """Submit a literal assertion through the proposal/policy path (SPEC §9).

        Evaluates ThresholdPolicy. Auto-accepted proposals are committed
        immediately with SPEC §10 conflict routing; others are stored for review.

        Conflict-routing temporality is resolved from the active schema's
        declared temporality for ``predicate`` (SPEC §10.1: ``t :=
        schema.temporality(P)``) — it is never caller-supplied. Falls back to
        "static" if no schema is registered for this namespace.

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

        schema = self.backend.get_schema(self.namespace)
        temporality: Literal["static", "time_varying"] = (
            schema.temporality_of(predicate) if schema is not None else "static"
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

        # SPEC §8.4: effective capability is min(author, acting_as) when
        # delegating, not a wholesale substitution (ADR-0003).
        decision = ThresholdPolicy().evaluate(proposal, principal, acting_as=delegating)

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

    def propose_ref(
        self,
        subject: str,
        predicate: str,
        target: str,
        author: str,
        *,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
        acting_as: str | None = None,
    ) -> tuple[Proposal, Decision]:
        """Submit a reference (relation) assertion through the proposal/policy path (SPEC §9).

        Mirrors ``propose()`` for relations — the only difference is the
        operation kind and that ``target`` (an entity ID) replaces
        ``value``/``value_type``. Evaluates ThresholdPolicy; auto-accepted
        proposals are committed immediately with SPEC §10 conflict routing.
        Conflict-routing temporality is resolved from the active schema's
        declared temporality for ``predicate`` (SPEC §10.1), never
        caller-supplied.

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

        schema = self.backend.get_schema(self.namespace)
        temporality: Literal["static", "time_varying"] = (
            schema.temporality_of(predicate) if schema is not None else "static"
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
                        "kind": "assert_ref",
                        "subject": subject,
                        "predicate": predicate,
                        "target": target,
                        "temporality": temporality,
                        "confidence": confidence,
                        "source": source,
                        "rationale": rationale,
                    }
                ]
            },
        )

        # SPEC §8.4: effective capability is min(author, acting_as) when
        # delegating, not a wholesale substitution (ADR-0003).
        decision = ThresholdPolicy().evaluate(proposal, principal, acting_as=delegating)

        if isinstance(decision, AutoAccept):
            assertion = Assertion(
                id=self.id_provider.next(),
                namespace=self.namespace,
                subject=subject,
                predicate=predicate,
                value_kind="ref",
                value=target,
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

        # SPEC §8.4: effective capability is min(author, acting_as) when
        # delegating, not a wholesale substitution (ADR-0003).
        decision = ThresholdPolicy().evaluate(proposal, principal, acting_as=delegating)

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
    ) -> Assertion:
        """Apply an assertion with SPEC §10 conflict routing. Must run inside a transaction.

        Returns the assertion as actually persisted (its ``status``/``supersedes``
        may differ from the input, e.g. when routing flags or supersedes it).
        """
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
            return final

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
            return flagged

        else:
            self.backend.put_assertion(assertion)
            return assertion

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
                elif op["kind"] == "assert_ref":
                    ref_assertion = Assertion(
                        id=self.id_provider.next(),
                        namespace=self.namespace,
                        subject=op["subject"],
                        predicate=op["predicate"],
                        value_kind="ref",
                        value=op["target"],
                        author=proposal.author,
                        confidence=op.get("confidence"),
                        source=op.get("source"),
                        rationale=op.get("rationale"),
                        asserted_at=now,
                        proposal_id=proposal_id,
                    )
                    self._apply_with_conflict_routing(
                        ref_assertion, op.get("temporality", "static")
                    )
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

    def flag_contradiction(
        self,
        assertion_id_a: str,
        assertion_id_b: str,
        author: str,
        *,
        rationale: str | None = None,
    ) -> tuple[Contradiction, str]:
        """Flag two assertions as contradictory, opening or extending a contradiction.

        Propose-level action (ADR-0008): author must hold >= propose
        capability. Both assertions are marked "flagged" and excluded from
        default queries until the contradiction is resolved. This does NOT
        resolve the contradiction — see resolve_contradiction().

        Args:
            assertion_id_a: First conflicting assertion ID
            assertion_id_b: Second conflicting assertion ID
            author: Principal ID raising the flag
            rationale: Optional explanation of the contradiction

        Returns:
            (Contradiction, action) where action is "created" or "extended"

        Raises:
            AuthError: author is not a known principal
            CapabilityError: author's capability is 'read'
            NotFoundError: either assertion id does not exist
            ValidationError: assertions do not share subject and predicate
        """
        principal = self.backend.get_principal(author)
        if principal is None:
            raise AuthError(f"Principal not found: {author}")
        if principal.default_capability == "read":
            raise CapabilityError(f"Principal {author!r} lacks propose capability")

        # Resolve both assertions (status=None: a flagged/superseded assertion
        # must still be resolvable here, e.g. when extending an open contradiction)
        all_assertions = self.backend.assertions(status=None)
        a_map = {a.id: a for a in all_assertions}

        a = a_map.get(assertion_id_a)
        b = a_map.get(assertion_id_b)
        if a is None:
            raise NotFoundError(f"Assertion not found: {assertion_id_a}")
        if b is None:
            raise NotFoundError(f"Assertion not found: {assertion_id_b}")
        if a.subject != b.subject or a.predicate != b.predicate:
            raise ValidationError(
                "Assertions must share the same subject and predicate to contradict"
            )

        existing = self.backend.get_open_contradiction(
            namespace=self.namespace,
            subject=a.subject,
            predicate=a.predicate,
        )

        with self.backend.transaction():
            if existing is not None:
                merged = list(dict.fromkeys(existing.member_ids + [assertion_id_a, assertion_id_b]))
                self.backend.update_contradiction_members(existing.id, merged)
                contradiction_id = existing.id
                action = "extended"
            else:
                contradiction_id = self.id_provider.next()
                self.backend.put_contradiction(
                    Contradiction(
                        id=contradiction_id,
                        namespace=self.namespace,
                        subject=a.subject,
                        predicate=a.predicate,
                        member_ids=[assertion_id_a, assertion_id_b],
                        state="open",
                        created_at=self.clock.now(),
                        metadata={"rationale": rationale} if rationale else {},
                    )
                )
                action = "created"

            for aid in (assertion_id_a, assertion_id_b):
                if a_map[aid].status != "flagged":
                    self.backend.set_assertion_status(aid, "flagged")

        result = self.backend.get_contradiction(contradiction_id)
        assert result is not None
        return result, action

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

    def issue_token(self, principal_id: str) -> str:
        """Issue a new API-key token for a principal (ADR-0014).

        Returns the raw secret ONCE — only its SHA-256 hash is persisted, and
        the raw value cannot be recovered afterward. Callers must save it
        immediately.

        Token generation uses ``secrets.token_urlsafe`` directly rather than
        the injected ``IdProvider``: this is a deliberate, documented
        exception to the "no non-determinism in domain logic" rule —
        cryptographic unpredictability is the entire point of a secret token,
        unlike ULIDs/timestamps, which are banned from domain logic for
        *reproducibility* reasons that don't apply here. The credential row's
        own id/created_at still go through id_provider/clock for that reason.

        Args:
            principal_id: Principal to issue a token for

        Returns:
            The raw token (not persisted anywhere — save it now)

        Raises:
            AuthError: principal_id does not name an existing principal
        """
        import secrets

        from ontolith.identity.token_auth import hash_token

        principal = self.backend.get_principal(principal_id)
        if principal is None:
            raise AuthError(f"Principal not found: {principal_id}")

        raw_token = secrets.token_urlsafe(32)
        credential = PrincipalCredential(
            id=self.id_provider.next(),
            principal_id=principal_id,
            token_hash=hash_token(raw_token),
            created_at=self.clock.now(),
        )
        self.backend.put_credential(credential)
        return raw_token

    def revoke_token(self, credential_id: str) -> None:
        """Revoke a previously issued token by its credential ID (ADR-0014).

        Args:
            credential_id: Credential to revoke (returned alongside the raw
                token by a token-issuance CLI/tool, not the token itself)

        Raises:
            NotFoundError: No credential with that ID exists
        """
        credential = self.backend.get_credential(credential_id)
        if credential is None:
            raise NotFoundError(f"Token credential not found: {credential_id}")
        self.backend.revoke_credential(credential_id, self.clock.now())

    def list_tokens(self, principal_id: str) -> list[PrincipalCredential]:
        """List all credentials (active and revoked) issued to a principal.

        Never returns the raw token or its hash — only id/created_at/revoked_at,
        enough to identify which credential to pass to ``revoke_token``.

        Args:
            principal_id: Principal to list credentials for

        Returns:
            Credentials for this principal, most recently issued first
        """
        return self.backend.get_credentials_for_principal(principal_id)

    def close(self) -> None:
        """Close the knowledge base connection."""
        self.backend.close()


__all__ = ["AsOfView", "Ontology"]
