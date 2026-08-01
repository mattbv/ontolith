"""Ontology - main entry point for the knowledge base.

The Ontology class is the primary API surface for users. It wraps the storage
backend and provides high-level methods for entities, assertions, and queries.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ontolith.core import (
    Assertion,
    AssertionEvent,
    Clock,
    Embedder,
    Entity,
    HashingEmbedder,
    IdProvider,
    Namespace,
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
from ontolith.govern.policy import Decision, PolicyStrategy, Reject
from ontolith.govern.proposal import Proposal, ProposalEvent
from ontolith.identity import Principal, PrincipalCredential, min_capability
from ontolith.query import QueryBuilder
from ontolith.schema import SchemaIR
from ontolith.store.base import DEFAULT_NAMESPACE, StorageBackend


class AsOfView:
    """Read-only bitemporal view at a specific point in time (SPEC §11.4).

    Reconstructs what was known and true at time `t`:
        valid_from <= t < (valid_to or ∞)  AND  asserted_at <= t

    Status is not used as a positive filter — the temporal dimensions
    determine visibility — but 'flagged' assertions (disputed, not
    confirmed-valid) are excluded by default, same as default (non-as_of)
    queries. Pass ``include_flagged=True`` for explicit audit/history views.
    """

    def __init__(
        self,
        backend: "StorageBackend",
        as_of: datetime,
        namespace: str,
        embedder: Embedder | None = None,
    ) -> None:
        self._backend = backend
        self._as_of = as_of
        self._namespace = namespace
        self._embedder = embedder

    def assertions(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        *,
        include_flagged: bool = False,
    ) -> list[Assertion]:
        """Assertions visible at the as_of timestamp."""
        return self._backend.assertions(
            subject=subject,
            predicate=predicate,
            status=None,
            as_of_time=self._as_of,
            include_flagged=include_flagged,
        )

    def query(self, concept: str) -> QueryBuilder:
        """Query entities as they existed at the as_of timestamp."""
        return QueryBuilder(
            backend=self._backend,
            namespace=self._namespace,
            concept=concept,
            as_of_time=self._as_of,
            embedder=self._embedder,
        )

    def schema(self) -> SchemaIR | None:
        """The schema version effective at the as_of timestamp (KI-019).

        Resolves via `StorageBackend.get_schema_at`, not `get_schema` (which
        always returns the latest version) — reconstructing what a
        property's temporality/cardinality meant at this point in time
        requires the schema that was actually in force then, not today's.

        Returns:
            Schema effective at this view's as_of time, or None if no
            version of this namespace's schema had been applied yet.
        """
        return self._backend.get_schema_at(self._namespace, self._as_of)


class Ontology:
    """Main knowledge base interface.

    This is the primary entry point for interacting with an Ontolith knowledge base.
    It provides methods for creating entities, making assertions, and querying.

    Example:
        >>> kb = Ontology.connect("my-kb.db")
        >>> alice = kb.create_principal(
        ...     "alice@example.com", kind="human", default_capability="write"
        ... )
        >>> entity = kb.create_entity("Person", author=alice.id)
        >>> assertion = kb.assert_literal(
        ...     entity.id, "Person.name", "Ada Lovelace", "Text", author=alice.id
        ... )
    """

    def __init__(
        self,
        backend: StorageBackend,
        clock: Clock | None = None,
        id_provider: IdProvider | None = None,
        policy: PolicyStrategy | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        """Initialize Ontology with a storage backend.

        Args:
            backend: Storage backend implementation
            clock: Clock for deterministic timestamps (defaults to SystemClock)
            id_provider: ID provider for deterministic IDs (defaults to UlidProvider)
            policy: Policy strategy for proposal evaluation (defaults to
                ThresholdPolicy — ADR-0018). ADR-0006 names PolicyStrategy
                as an open-core extension point for proprietary strategies.
            embedder: Embedder for .semantic() queries and reindex() (defaults
                to HashingEmbedder — ADR-0020).
        """
        self.backend = backend
        self.clock = clock or SystemClock()
        self.id_provider = id_provider or UlidProvider()
        self.policy = policy or ThresholdPolicy()
        self.embedder = embedder or HashingEmbedder()
        self.namespace = DEFAULT_NAMESPACE  # For M1, single namespace

    @classmethod
    def connect(
        cls,
        path: str | Path,
        *,
        clock: Clock | None = None,
        id_provider: IdProvider | None = None,
        policy: PolicyStrategy | None = None,
        embedder: Embedder | None = None,
    ) -> "Ontology":
        """Connect to a knowledge base.

        Args:
            path: Path to SQLite database file
            clock: Optional clock for deterministic behavior
            id_provider: Optional ID provider for deterministic behavior
            policy: Optional policy strategy (defaults to ThresholdPolicy —
                ADR-0018)
            embedder: Optional Embedder (defaults to HashingEmbedder)

        Returns:
            Ontology instance connected to the database
        """
        from ontolith.store.sqlite import SQLiteBackend

        effective_clock = clock or SystemClock()
        backend = SQLiteBackend(path, clock=effective_clock)
        return cls(
            backend,
            clock=effective_clock,
            id_provider=id_provider,
            policy=policy,
            embedder=embedder,
        )

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

        Raises:
            ValidationError: kind is "ai" and owner is missing, or doesn't
                name an existing human/service principal (SPEC §8.1: AI
                principals must declare a *resolvable* accountable owner,
                not just a non-null string)
        """
        if kind == "ai" and owner is None:
            # Principal's own model_validator also enforces this, but
            # raises pydantic's ValidationError, not this method's
            # documented ontolith ValidationError - checked explicitly
            # here so every caller (including REST, which maps error
            # *types* to HTTP statuses) sees one consistent exception.
            raise ValidationError("AI principals must have an owner (SPEC §8.1)")

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

        if principal.kind == "ai":
            assert principal.owner is not None  # enforced by Principal's model_validator
            owner_principal = self.backend.get_principal(principal.owner)
            if owner_principal is None:
                raise ValidationError(f"AI principal owner not found: {principal.owner!r}")
            if owner_principal.kind == "ai":
                raise ValidationError(
                    f"AI principal owner must be human or service, not ai: {principal.owner!r}"
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

        Raises:
            AuthError: author is not a known principal
            CapabilityError: author's capability is 'read'
        """
        principal = self.backend.get_principal(author)
        if principal is None:
            raise AuthError(f"Principal not found: {author}")
        if principal.default_capability == "read":
            raise CapabilityError(f"Principal {author!r} lacks propose capability")

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

    def _get_principal_or_raise(self, principal_id: str) -> Principal:
        """Look up `principal_id`, raising AuthError if unknown."""
        principal = self.backend.get_principal(principal_id)
        if principal is None:
            raise AuthError(f"Principal not found: {principal_id}")
        return principal

    def _resolve_delegation(
        self, principal: Principal, author: str, acting_as: str | None
    ) -> Principal | None:
        """Resolve and authorize the delegating principal for `acting_as` (ADR-0003).

        Returns None when `acting_as` is absent or equals `author` (no
        delegation). `author` must be owned by `acting_as` to delegate.

        Raises:
            AuthError: acting_as is not a known principal
            CapabilityError: author is not owned by acting_as
        """
        if acting_as is None or acting_as == author:
            return None
        delegating = self.backend.get_principal(acting_as)
        if delegating is None:
            raise AuthError(f"Delegating principal not found: {acting_as}")
        if principal.owner != acting_as:
            raise CapabilityError(f"Principal {author!r} is not authorized to act as {acting_as!r}")
        return delegating

    def _finalize_non_accepted_decision(
        self, proposal: Proposal, decision: Decision, now: datetime, *, is_new: bool = True
    ) -> tuple[Proposal, Decision] | None:
        """Persist and return the Reject/require-review outcome shared by
        propose/propose_ref/retract/resubmit. Returns None for AutoAccept,
        signaling the caller must still apply the operation-specific side
        effects.

        `is_new` selects how `proposal` is persisted: INSERT
        (`put_proposal`) for a proposal not yet written (propose/
        propose_ref/retract), or UPDATE (`update_proposal_state`) for an
        existing row being re-decided (`resubmit`, KI-027) — `put_proposal`
        would raise on the row's already-existing id. `update_proposal_state`
        clears the row's `decided_at` unconditionally (it is not
        COALESCE'd like `policy_reason`), so persistence is correct either
        way; callers passing `is_new=False` must still reset `proposal.
        decided_at` to None on their own in-memory copy before calling, or
        the object this method *returns* will disagree with the row it just
        wrote (a resubmitted proposal that lands back in require_review is,
        again, not yet decided).
        """
        if isinstance(decision, Reject):
            rejected = proposal.model_copy(
                update={"state": "rejected", "decided_at": now, "policy_reason": decision.reason}
            )
            if is_new:
                self.backend.put_proposal(rejected)
            else:
                self.backend.update_proposal_state(
                    proposal.id, "rejected", now.isoformat(), decision.reason
                )
            return rejected, decision

        if not isinstance(decision, AutoAccept):
            pending = proposal.model_copy(
                update={
                    "state": "require_review",
                    "policy_reason": getattr(decision, "reason", None),
                }
            )
            if is_new:
                self.backend.put_proposal(pending)
            else:
                self.backend.update_proposal_state(
                    proposal.id, "require_review", policy_reason=getattr(decision, "reason", None)
                )
            return pending, decision

        return None

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
        principal = self._get_principal_or_raise(author)
        if principal.kind == "ai":
            raise CapabilityError(f"AI principal {author!r} cannot make direct writes")

        delegating = self._resolve_delegation(principal, author, acting_as)

        # SPEC §8.4: effective capability is min(author, acting_as) when
        # delegating, not a wholesale substitution.
        capability: str = principal.default_capability
        if delegating is not None:
            capability = min_capability(capability, delegating.default_capability)
        if capability not in ("write", "admin"):
            raise CapabilityError(f"Principal {author!r} lacks write capability")

        return principal, delegating

    def _resolve_temporality(self, predicate: str) -> Literal["static", "time_varying"]:
        """Look up a predicate's temporality, defaulting to static (SPEC §10.1)."""
        schema = self.backend.get_schema(self.namespace)
        return schema.temporality_of(predicate) if schema is not None else "static"

    def _resolve_cardinality(self, predicate: str) -> Literal["single", "many"]:
        """Look up a predicate's cardinality, defaulting to single (ADR-0017)."""
        schema = self.backend.get_schema(self.namespace)
        return schema.cardinality_of(predicate) if schema is not None else "single"

    def _require_known_predicate(self, predicate: str) -> None:
        """Reject an unknown predicate at write time (SPEC §4) rather than
        silently defaulting its temporality/cardinality to static/single.

        No-op when no schema is registered for the namespace yet — a
        schema-less namespace has nothing to validate a predicate against.
        """
        schema = self.backend.get_schema(self.namespace)
        if schema is not None and not schema.has_predicate(predicate):
            raise ValidationError(
                f"Unknown predicate {predicate!r}: not declared in schema "
                f"{schema.namespace!r} version {schema.version}"
            )

    def _retraction_valid_to(self, assertion_id: str, now: datetime) -> str | None:
        """Compute valid_to for a retraction.

        Closes an open window at `now`, but never widens a window already
        closed by a prior supersession/retraction — retraction must only
        ever narrow validity, never rewrite history (bitemporal.md #1-2).
        """
        current = self.backend.get_assertion(assertion_id)
        if current is not None and current.valid_to is not None:
            return None
        return now.isoformat()

    @staticmethod
    def _parse_window(op: dict[str, Any], key: str) -> datetime | None:
        """Parse a valid_from/valid_to ISO string back out of a proposal
        operation payload (propose()/propose_ref() serialize datetimes to
        strings since Proposal.payload is a JSON-compatible dict)."""
        raw = op.get(key)
        return datetime.fromisoformat(raw) if raw else None

    def _record_assertion_event(
        self,
        assertion_id: str,
        actor: str,
        action: Literal["superseded", "flagged", "retracted", "reactivated"],
        at: datetime,
        successor_id: str | None = None,
    ) -> None:
        """Append an audit event for an assertion status mutation.

        Must run inside the same transaction as the corresponding
        set_assertion_status call (append-only invariant: the event log and
        the status it describes must never diverge).

        Args:
            successor_id: For action="superseded", the id of the assertion
                that caused it — recovers the full predecessor set when one
                incoming assertion supersedes several at once (KI-008),
                since Assertion.supersedes only records the first.
        """
        self.backend.put_assertion_event(
            AssertionEvent(
                id=self.id_provider.next(),
                assertion_id=assertion_id,
                actor=actor,
                action=action,
                at=at,
                successor_id=successor_id,
            )
        )

    @staticmethod
    def _require_model_for_ai(principal: Principal, model: str | None) -> None:
        """SPEC §7.4/§14.4 MUST: AI-authored assertions carry model provenance."""
        if principal.kind == "ai" and model is None:
            raise ValidationError(
                f"model is required for ai-kind authors (principal: {principal.id})"
            )

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
        model: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
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
            model: Model family+version (AI principals cannot reach this
                direct-write path — see _check_direct_write_capability — so
                this is accepted but never required here)
            valid_from: When the fact became/becomes true (defaults to now —
                SPEC §5.3). Set explicitly to backfill historical windows,
                e.g. time_varying employment history.
            valid_to: When the fact stopped being true (defaults to open/None)

        Returns:
            Assertion as persisted (status/supersedes reflect conflict routing)
        """
        self._check_direct_write_capability(author, acting_as)
        self._require_known_predicate(predicate)
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
            model=model,
            asserted_at=self.clock.now(),
            valid_from=valid_from,
            valid_to=valid_to,
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
        model: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
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
            model: Model family+version (AI principals cannot reach this
                direct-write path — see _check_direct_write_capability — so
                this is accepted but never required here)
            valid_from: When the fact became/becomes true (defaults to now —
                SPEC §5.3). Set explicitly to backfill historical windows,
                e.g. time_varying employment history.
            valid_to: When the fact stopped being true (defaults to open/None)

        Returns:
            Assertion as persisted (status/supersedes reflect conflict routing)
        """
        self._check_direct_write_capability(author, acting_as)
        self._require_known_predicate(predicate)
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
            model=model,
            asserted_at=self.clock.now(),
            valid_from=valid_from,
            valid_to=valid_to,
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
            embedder=self.embedder,
        )

    def as_of(self, t: datetime | str) -> AsOfView:
        """Return a read-only bitemporal view at time t (SPEC §11.4).

        Reconstructs what was known and true at t:
            valid_from <= t < (valid_to or ∞)  AND  asserted_at <= t

        Args:
            t: Point in time — datetime or ISO-format string. Naive values
                (no tzinfo) are treated as UTC, matching Clock's contract
                that all stored timestamps are UTC — otherwise a naive `t`
                would be compared against UTC-aware stored timestamps as
                a plain ISO string, silently misordering results instead
                of erroring.

        Returns:
            AsOfView for querying the knowledge base as it stood at t
        """
        if isinstance(t, str):
            t = datetime.fromisoformat(t)
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        return AsOfView(self.backend, t, self.namespace, embedder=self.embedder)

    def reindex(self, concept: str | None = None) -> int:
        """Re-embed entities' Text content into the vector index (SPEC §11.3).

        No write path (propose/accept_proposal) auto-embeds on write — this
        is the only way vectors enter the index (ADR-0020 amendment). Safe
        to call repeatedly: each call re-embeds and upserts, so it is
        idempotent and picks up any Text assertions added since the last
        call.

        For each entity, concatenates its Text-typed active-assertion values
        (sorted by predicate then asserted_at) into one string and embeds
        it. Entities with no Text-typed active assertions are skipped, not
        zero-vector-upserted — a zero vector would spuriously rank as
        "close" to other empty entities in `.semantic()` results.

        Args:
            concept: If set, only re-index entities of this concept.
                Otherwise all entities in this namespace.

        Returns:
            Number of entities actually embedded (excludes skipped ones).
        """
        entities = self.backend.entities(namespace=self.namespace, concept=concept)

        indexed_entities = []
        texts = []
        for entity in entities:
            text = self._entity_text(entity)
            if text is None:
                continue
            indexed_entities.append(entity)
            texts.append(text)

        if not texts:
            return 0

        vectors = self.embedder.embed(texts)
        for entity, vector in zip(indexed_entities, vectors, strict=True):
            self.backend.vector_upsert("entity", entity.id, vector)

        return len(indexed_entities)

    def _entity_text(self, entity: Entity) -> str | None:
        """Concatenate an entity's Text-typed active assertion values.

        Returns None if the entity has no Text-typed active assertions.
        """
        text_assertions = [
            a
            for a in self.backend.assertions(subject=entity.id, status="active")
            if a.value_type == "Text"
        ]
        if not text_assertions:
            return None
        text_assertions.sort(key=lambda a: (a.predicate, a.asserted_at))
        return " ".join(a.value for a in text_assertions)

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
        model: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> tuple[Proposal, Decision]:
        """Submit a literal assertion through the proposal/policy path (SPEC §9).

        Evaluates ``self.policy`` (``ThresholdPolicy`` by default — ADR-0018).
        Auto-accepted proposals are committed immediately with SPEC §10
        conflict routing; others are stored for review.

        Conflict-routing temporality is resolved from the active schema's
        declared temporality for ``predicate`` (SPEC §10.1: ``t :=
        schema.temporality(P)``) — it is never caller-supplied. Falls back to
        "static" if no schema is registered for this namespace.

        ``model`` (the AI model family+version) is REQUIRED when ``author``
        is an ``ai``-kind principal (SPEC §7.4/§14.4).

        When ``acting_as`` is set the proposal is made on behalf of another
        principal (delegation, ADR-0003). Policy is evaluated using the
        delegating principal's capability and trust level.

        Args:
            valid_from: When the fact became/becomes true (defaults to now —
                SPEC §5.3). Set explicitly to backfill historical windows.
            valid_to: When the fact stopped being true (defaults to open/None)

        Returns:
            (Proposal, Decision) tuple

        Raises:
            AuthError: author or acting_as is not a known principal
            CapabilityError: delegation is unauthorized
            ValidationError: author is ai-kind and model is not provided, or
                predicate is not declared in the active schema
        """
        principal = self._get_principal_or_raise(author)
        self._require_model_for_ai(principal, model)
        self._require_known_predicate(predicate)
        delegating = self._resolve_delegation(principal, author, acting_as)
        temporality = self._resolve_temporality(predicate)

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
                        "model": model,
                        "valid_from": valid_from.isoformat() if valid_from else None,
                        "valid_to": valid_to.isoformat() if valid_to else None,
                    }
                ]
            },
        )

        # SPEC §8.4: effective capability is min(author, acting_as) when
        # delegating, not a wholesale substitution (ADR-0003).
        # kb pinned at `now` (== proposal.created_at): nothing from this
        # proposal is persisted yet, so at evaluation time it can't see its
        # own operation (KI-017, ADR-0025). Replaying as_of(proposal.
        # created_at) later reproduces this same read only if nothing else
        # was committed at exactly that timestamp afterward — asserted_at <=
        # t is inclusive of t, so a same-tick write IS visible on replay.
        kb_view = self.as_of(now)
        decision = self.policy.evaluate(proposal, principal, kb_view, acting_as=delegating)
        finalized = self._finalize_non_accepted_decision(proposal, decision, now)
        if finalized is not None:
            return finalized
        assert isinstance(decision, AutoAccept)

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
            model=model,
            asserted_at=now,
            proposal_id=proposal_id,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        accepted = proposal.model_copy(
            update={"state": "auto_accepted", "decided_at": now, "policy_reason": decision.reason}
        )
        with self.backend.transaction():
            self.backend.put_proposal(accepted)
            self._apply_with_conflict_routing(assertion, temporality)
        return accepted, decision

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
        model: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> tuple[Proposal, Decision]:
        """Submit a reference (relation) assertion through the proposal/policy path (SPEC §9).

        Mirrors ``propose()`` for relations — the only difference is the
        operation kind and that ``target`` (an entity ID) replaces
        ``value``/``value_type``. Evaluates ``self.policy`` (``ThresholdPolicy``
        by default — ADR-0018); auto-accepted proposals are committed
        immediately with SPEC §10 conflict routing. Conflict-routing
        temporality is resolved from the active schema's declared
        temporality for ``predicate`` (SPEC §10.1), never caller-supplied.

        ``model`` (the AI model family+version) is REQUIRED when ``author``
        is an ``ai``-kind principal (SPEC §7.4/§14.4).

        When ``acting_as`` is set the proposal is made on behalf of another
        principal (delegation, ADR-0003). Policy is evaluated using the
        delegating principal's capability and trust level.

        Args:
            valid_from: When the fact became/becomes true (defaults to now —
                SPEC §5.3). Set explicitly to backfill historical windows.
            valid_to: When the fact stopped being true (defaults to open/None)

        Returns:
            (Proposal, Decision) tuple

        Raises:
            AuthError: author or acting_as is not a known principal
            CapabilityError: delegation is unauthorized
            ValidationError: author is ai-kind and model is not provided, or
                predicate is not declared in the active schema
        """
        principal = self._get_principal_or_raise(author)
        self._require_model_for_ai(principal, model)
        self._require_known_predicate(predicate)
        delegating = self._resolve_delegation(principal, author, acting_as)
        temporality = self._resolve_temporality(predicate)

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
                        "model": model,
                        "valid_from": valid_from.isoformat() if valid_from else None,
                        "valid_to": valid_to.isoformat() if valid_to else None,
                    }
                ]
            },
        )

        # SPEC §8.4: effective capability is min(author, acting_as) when
        # delegating, not a wholesale substitution (ADR-0003).
        # kb pinned at `now` (== proposal.created_at) — see propose()'s
        # comment for the replay caveat (KI-017, ADR-0025).
        kb_view = self.as_of(now)
        decision = self.policy.evaluate(proposal, principal, kb_view, acting_as=delegating)
        finalized = self._finalize_non_accepted_decision(proposal, decision, now)
        if finalized is not None:
            return finalized
        assert isinstance(decision, AutoAccept)

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
            model=model,
            asserted_at=now,
            proposal_id=proposal_id,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        accepted = proposal.model_copy(
            update={"state": "auto_accepted", "decided_at": now, "policy_reason": decision.reason}
        )
        with self.backend.transaction():
            self.backend.put_proposal(accepted)
            self._apply_with_conflict_routing(assertion, temporality)
        return accepted, decision

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
        principal = self._get_principal_or_raise(author)
        delegating = self._resolve_delegation(principal, author, acting_as)

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
        # kb pinned at `now` (== proposal.created_at) — see propose()'s
        # comment for the replay caveat (KI-017, ADR-0025).
        kb_view = self.as_of(now)
        decision = self.policy.evaluate(proposal, principal, kb_view, acting_as=delegating)
        finalized = self._finalize_non_accepted_decision(proposal, decision, now)
        if finalized is not None:
            return finalized
        assert isinstance(decision, AutoAccept)

        accepted = proposal.model_copy(
            update={"state": "auto_accepted", "decided_at": now, "policy_reason": decision.reason}
        )
        with self.backend.transaction():
            self.backend.put_proposal(accepted)
            self.backend.set_assertion_status(
                assertion_id, "retracted", valid_to=self._retraction_valid_to(assertion_id, now)
            )
            self._record_assertion_event(assertion_id, author, "retracted", now)
        return accepted, decision

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
                cardinality=self._resolve_cardinality(assertion.predicate),
            )

        if isinstance(result, Supersede):
            # Close prior window at the incoming assertion's valid_from (SPEC §10.2).
            # valid_from is guaranteed non-None after Assertion validation.
            close_at = (assertion.valid_from or assertion.asserted_at).isoformat()
            supersedes_id = result.targets[0] if result.targets else None
            final = assertion.model_copy(update={"supersedes": supersedes_id})
            # Persisted before the loop below: each superseded-event row's
            # successor_id FK (SQLite) references this row, so it must exist
            # first. Safe — both inserts share this method's transaction.
            self.backend.put_assertion(final)
            for target_id in result.targets:
                self.backend.set_assertion_status(target_id, "superseded", valid_to=close_at)
                self._record_assertion_event(
                    target_id,
                    assertion.author,
                    "superseded",
                    assertion.asserted_at,
                    successor_id=final.id,
                )
            return final

        elif isinstance(result, Contradict):
            for mid in result.member_ids:
                if mid != assertion.id:
                    # Extending an already-open contradiction re-flags members
                    # that are already flagged (idempotent status write) — only
                    # emit an event for an actual transition, not a no-op.
                    already_flagged = mid in (
                        open_contradiction.member_ids if open_contradiction else []
                    )
                    self.backend.set_assertion_status(mid, "flagged")
                    if not already_flagged:
                        self._record_assertion_event(
                            mid, assertion.author, "flagged", assertion.asserted_at
                        )
            flagged = assertion.model_copy(update={"status": "flagged"})
            self.backend.put_assertion(flagged)
            # The incoming assertion is created already-flagged, but it still
            # needs its own event: as_of() reconstructs flagged-status-at-t
            # from assertion_event, and an assertion with zero events looks
            # like it was never flagged, silently hiding pre-dispute history.
            self._record_assertion_event(
                flagged.id, assertion.author, "flagged", assertion.asserted_at
            )
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
                    raised_by=assertion.author,
                )
                self.backend.put_contradiction(contradiction)
            return flagged

        else:
            self.backend.put_assertion(assertion)
            return assertion

    def _require_reviewer(self, proposal_id: str, reviewer: str) -> Proposal:
        """Shared eligibility gate for accept_proposal/reject_proposal/request_changes (SPEC §9.4).

        The reviewer must have `review` or `admin` capability, must not be
        an AI principal (ThresholdPolicy always routes AI proposals to
        require_review — an AI reviewer would defeat that guarantee), and
        must not be the proposal's own author or delegating principal
        (self-review would let a misconfigured AI principal with `review`
        capability, or a delegate reviewing their own delegated proposal,
        approve its own work). The proposal must exist and be in
        `require_review` or `under_review` state.

        Args:
            proposal_id: ID of the proposal being reviewed
            reviewer: Principal ID of the reviewer

        Returns:
            The proposal being reviewed

        Raises:
            AuthError: reviewer is not a known principal
            NotFoundError: proposal_id does not name an existing proposal
            CapabilityError: reviewer lacks review/admin capability, is
                AI-kind, or is the proposal's own author/delegate
            ValidationError: proposal is not pending review
        """
        reviewer_principal = self.backend.get_principal(reviewer)
        if reviewer_principal is None:
            raise AuthError(f"Principal not found: {reviewer}")
        if reviewer_principal.default_capability not in ("review", "admin"):
            raise CapabilityError(f"Principal {reviewer} lacks review capability")
        if reviewer_principal.kind == "ai":
            raise CapabilityError(f"Principal {reviewer!r} is an AI principal and cannot review")

        proposal = self.backend.get_proposal(proposal_id)
        if proposal is None:
            raise NotFoundError(f"Proposal not found: {proposal_id}")
        if reviewer in (proposal.author, proposal.acting_as):
            raise CapabilityError(f"Principal {reviewer!r} cannot review their own proposal")
        if proposal.state not in ("require_review", "under_review"):
            raise ValidationError(
                f"Proposal {proposal_id} is not pending review (state: {proposal.state})"
            )
        return proposal

    def _replay_proposal_operations(self, proposal: Proposal, now: datetime) -> None:
        """Apply a proposal's staged operations through SPEC §10 conflict
        routing. Shared by `accept_proposal` and `resubmit` (KI-027) — MUST
        be called inside an open `backend.transaction()`.

        Temporality is re-resolved dynamically against the current schema
        for each operation's predicate, never trusted from the payload's
        stored snapshot (`TestAcceptProposalReResolvesTemporality`) — the
        schema may have changed between proposal creation and replay.
        """
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
                    acting_as=proposal.acting_as,
                    confidence=op.get("confidence"),
                    source=op.get("source"),
                    rationale=op.get("rationale"),
                    model=op.get("model"),
                    asserted_at=now,
                    proposal_id=proposal.id,
                    valid_from=self._parse_window(op, "valid_from"),
                    valid_to=self._parse_window(op, "valid_to"),
                )
                self._apply_with_conflict_routing(
                    assertion, self._resolve_temporality(op["predicate"])
                )
            elif op["kind"] == "assert_ref":
                ref_assertion = Assertion(
                    id=self.id_provider.next(),
                    namespace=self.namespace,
                    subject=op["subject"],
                    predicate=op["predicate"],
                    value_kind="ref",
                    value=op["target"],
                    author=proposal.author,
                    acting_as=proposal.acting_as,
                    confidence=op.get("confidence"),
                    source=op.get("source"),
                    rationale=op.get("rationale"),
                    model=op.get("model"),
                    asserted_at=now,
                    proposal_id=proposal.id,
                    valid_from=self._parse_window(op, "valid_from"),
                    valid_to=self._parse_window(op, "valid_to"),
                )
                self._apply_with_conflict_routing(
                    ref_assertion, self._resolve_temporality(op["predicate"])
                )
            elif op["kind"] == "retract":
                self.backend.set_assertion_status(
                    op["assertion_id"],
                    "retracted",
                    valid_to=self._retraction_valid_to(op["assertion_id"], now),
                )
                self._record_assertion_event(op["assertion_id"], proposal.author, "retracted", now)
            else:
                raise ValidationError(f"Unknown operation kind in proposal payload: {op['kind']}")

    def accept_proposal(self, proposal_id: str, reviewer: str) -> Proposal:
        """Accept a pending proposal, replaying its operations (SPEC §9).

        See `_require_reviewer` for the reviewer-eligibility and
        proposal-state checks shared with `reject_proposal`/
        `request_changes`. Operations are replayed through SPEC §10
        conflict routing inside a single transaction.

        Args:
            proposal_id: ID of the proposal to accept
            reviewer: Principal ID of the reviewer

        Returns:
            Updated Proposal with state `accepted`

        Raises:
            AuthError: reviewer is not a known principal
            NotFoundError: proposal_id does not name an existing proposal
            CapabilityError: reviewer lacks review/admin capability, is
                AI-kind, or is the proposal's own author/delegate
            ValidationError: proposal is not pending review
        """
        proposal = self._require_reviewer(proposal_id, reviewer)

        now = self.clock.now()

        with self.backend.transaction():
            self._replay_proposal_operations(proposal, now)
            self.backend.update_proposal_state(proposal_id, "accepted", now.isoformat())
            self.backend.put_proposal_event(
                ProposalEvent(
                    id=self.id_provider.next(),
                    proposal_id=proposal_id,
                    actor=reviewer,
                    type="accept",
                    at=now,
                )
            )

        accepted = self.backend.get_proposal(proposal_id)
        assert accepted is not None
        return accepted

    def reject_proposal(self, proposal_id: str, reviewer: str, reason: str = "") -> Proposal:
        """Reject a pending proposal (SPEC §9).

        See `_require_reviewer` for the reviewer-eligibility and
        proposal-state checks shared with `accept_proposal`/
        `request_changes`. No operations are applied; the proposal is
        marked rejected.

        Args:
            proposal_id: ID of the proposal to reject
            reviewer: Principal ID of the reviewer
            reason: Optional rejection reason

        Returns:
            Updated Proposal with state `rejected`

        Raises:
            AuthError: reviewer is not a known principal
            NotFoundError: proposal_id does not name an existing proposal
            CapabilityError: reviewer lacks review/admin capability, is
                AI-kind, or is the proposal's own author/delegate
            ValidationError: proposal is not pending review
        """
        self._require_reviewer(proposal_id, reviewer)

        now = self.clock.now()
        with self.backend.transaction():
            self.backend.update_proposal_state(proposal_id, "rejected", now.isoformat())
            self.backend.put_proposal_event(
                ProposalEvent(
                    id=self.id_provider.next(),
                    proposal_id=proposal_id,
                    actor=reviewer,
                    type="reject",
                    detail=reason or None,
                    at=now,
                )
            )

        rejected = self.backend.get_proposal(proposal_id)
        assert rejected is not None
        return rejected

    def request_changes(self, proposal_id: str, reviewer: str, reason: str = "") -> Proposal:
        """Request changes on a pending proposal (SPEC §9.1/§9.4).

        See `_require_reviewer` for the reviewer-eligibility and
        proposal-state checks shared with `accept_proposal`/
        `reject_proposal`. No operations are applied; the proposal moves to
        `changes_requested` — SPEC §9.1's third `under_review` outcome,
        alongside `accepted`/`rejected`. The proposal's own author or
        delegate can move it back to `submitted` via `resubmit` (KI-027).

        Args:
            proposal_id: ID of the proposal
            reviewer: Principal ID of the reviewer
            reason: Optional explanation of what needs to change

        Returns:
            Updated Proposal with state `changes_requested`

        Raises:
            AuthError: reviewer is not a known principal
            NotFoundError: proposal_id does not name an existing proposal
            CapabilityError: reviewer lacks review/admin capability, is
                AI-kind, or is the proposal's own author/delegate
            ValidationError: proposal is not pending review
        """
        self._require_reviewer(proposal_id, reviewer)

        now = self.clock.now()
        with self.backend.transaction():
            self.backend.update_proposal_state(proposal_id, "changes_requested", now.isoformat())
            self.backend.put_proposal_event(
                ProposalEvent(
                    id=self.id_provider.next(),
                    proposal_id=proposal_id,
                    actor=reviewer,
                    type="request_changes",
                    detail=reason or None,
                    at=now,
                )
            )

        updated = self.backend.get_proposal(proposal_id)
        assert updated is not None
        return updated

    def resubmit(self, proposal_id: str, author: str) -> tuple[Proposal, Decision]:
        """Resubmit a proposal after changes were requested (SPEC §9.1:
        `changes_requested -> submitted -> {policy}`), closing the dead end
        `request_changes` previously left (KI-027).

        Only the proposal's own author or delegating principal may call
        this — the inverse of `_require_reviewer`'s self-review guard: this
        is an author action, not a reviewer one. The payload is replayed
        unedited (in-place payload editing before resubmission is not yet
        supported); policy is re-evaluated against a fresh `kb_view`
        pinned at the resubmission instant — unlike `propose`/
        `propose_ref`, that pin is deliberately NOT `proposal.created_at`
        (ADR-0025): the proposal already exists, so a KB-reading
        `PolicyStrategy` (e.g. `SourceQuorum`) must evaluate it against
        what's true now, not what was true when it was first drafted.
        `_require_model_for_ai`/`_require_known_predicate` are
        deliberately not re-run — the original submission's shape is
        trusted, matching `accept_proposal`'s existing precedent — but
        temporality is re-resolved dynamically at replay time on
        auto-accept (see `_replay_proposal_operations`).

        A `ProposalEvent(type="resubmit")` is always recorded, regardless
        of outcome — unlike `propose`'s own auto-accept path (which
        records none for a brand-new proposal), `resubmit` re-decides an
        already-persisted row, and without an event a `require_review`
        outcome would otherwise leave no trace of when policy last ran
        (`decided_at` stays `None`, `created_at` stays the original
        submission time).

        Args:
            proposal_id: ID of the proposal to resubmit
            author: Principal ID resubmitting (must be the proposal's own
                author or delegating principal)

        Returns:
            (Proposal, Decision) tuple, matching propose()/propose_ref()

        Raises:
            AuthError: author, or the proposal's original author/delegate
                (if since removed), is not a known principal
            NotFoundError: proposal_id does not name an existing proposal
            CapabilityError: author is not the proposal's own author/delegate
            ValidationError: proposal is not awaiting resubmission
        """
        caller = self._get_principal_or_raise(author)
        proposal = self.backend.get_proposal(proposal_id)
        if proposal is None:
            raise NotFoundError(f"Proposal not found: {proposal_id}")
        if author not in (proposal.author, proposal.acting_as):
            raise CapabilityError(
                f"Principal {author!r} did not author proposal {proposal_id} and cannot resubmit it"
            )
        if proposal.state != "changes_requested":
            raise ValidationError(
                f"Proposal {proposal_id} is not awaiting resubmission (state: {proposal.state})"
            )

        principal = (
            caller if author == proposal.author else self._get_principal_or_raise(proposal.author)
        )
        delegating = self._resolve_delegation(principal, proposal.author, proposal.acting_as)

        now = self.clock.now()
        # decided_at is reset to None: the changes_requested -> submitted
        # transition re-opens the decision, it doesn't carry the prior
        # request_changes decision forward.
        resubmitted = proposal.model_copy(update={"state": "submitted", "decided_at": None})
        kb_view = self.as_of(now)
        decision = self.policy.evaluate(resubmitted, principal, kb_view, acting_as=delegating)

        with self.backend.transaction():
            finalized = self._finalize_non_accepted_decision(
                resubmitted, decision, now, is_new=False
            )
            if finalized is not None:
                result, decision = finalized
            else:
                assert isinstance(decision, AutoAccept)
                result = resubmitted.model_copy(
                    update={
                        "state": "auto_accepted",
                        "decided_at": now,
                        "policy_reason": decision.reason,
                    }
                )
                self.backend.update_proposal_state(
                    proposal_id, "auto_accepted", now.isoformat(), decision.reason
                )
                self._replay_proposal_operations(result, now)

            self.backend.put_proposal_event(
                ProposalEvent(
                    id=self.id_provider.next(),
                    proposal_id=proposal_id,
                    actor=author,
                    type="resubmit",
                    detail=getattr(decision, "reason", None),
                    at=now,
                )
            )

        return result, decision

    def proposals(self, state: str | None = "require_review") -> list[Proposal]:
        """List proposals, defaulting to those pending review (SPEC §14.1).

        Without this, `route_to_review` (SPEC §10.3) has no way to surface
        what it routed — a reviewer would need direct backend access to
        discover pending proposals.

        Args:
            state: Filter by proposal state. `"pending"` is a query-level
                alias for `require_review` OR `changes_requested` combined —
                both are still-open proposals needing someone's attention (a
                reviewer for the former, the author for the latter). It is
                NOT the default: a canonical reviewer loop
                (`for p in kb.proposals(): kb.accept_proposal(p.id, ...)`)
                assumes every returned proposal is actionable by a reviewer,
                which is only true of `require_review` —
                `changes_requested` proposals raise `ValidationError` from
                `accept_proposal`/`reject_proposal` (KI-027). Pass
                `state="changes_requested"` or `state="pending"` explicitly
                to include them. `None` returns every state.

        Returns:
            Matching proposals, most recently created first
        """
        if state == "pending":
            merged = self.backend.proposals(state="require_review")
            merged += self.backend.proposals(state="changes_requested")
            merged.sort(key=lambda p: (p.created_at, p.id), reverse=True)
            return merged
        return self.backend.proposals(state=state)

    def contradictions(self, state: str | None = "open") -> list[Contradiction]:
        """List contradictions, defaulting to open (unresolved) ones (SPEC §14.1).

        Args:
            state: Filter by contradiction state ("open" or "resolved");
                None returns every state

        Returns:
            Matching contradictions, most recently created first
        """
        return self.backend.contradictions(state=state)

    def list_namespaces(self) -> list[Namespace]:
        """List all registered namespaces (SPEC §12.2, KI-022).

        Ungated, like `proposals()`/`contradictions()` — namespace metadata
        (id, creation time) carries no sensitive content comparable to
        `list_principals()`'s `owner`/`trust_level` fields. This project is
        still single-namespace throughout (ADR-0015): today this always
        returns exactly one entry, `DEFAULT_NAMESPACE`, seeded by every
        backend at schema-creation time.

        Returns:
            All namespaces, most recently created first
        """
        return self.backend.list_namespaces()

    def resolve_contradiction(
        self,
        contradiction_id: str,
        winner_assertion_id: str,
        resolver: str,
    ) -> Contradiction:
        """Resolve an open contradiction by selecting a winning assertion (SPEC §10.3).

        All other member assertions are retracted; the winning assertion is
        reactivated. The resolver must have `review` or `admin` capability
        and must not be an AI principal (ThresholdPolicy always routes AI
        proposals to require_review; an AI resolver would let it approve
        its own or another AI's disputed value unsupervised). The resolver
        also must not be the author or delegate of *any* member assertion
        (KI-026) — not just the winner, since an interested party shouldn't
        get to pick against their own losing entry either. Without this, a
        principal who authored one side of a disputed static fact could
        adjudicate the dispute in their own favor unilaterally. This check
        is deliberately narrower than "any AI member's owner" — an AI
        principal's accountable owner is still eligible to resolve a
        contradiction that AI is party to, consistent with `owner` already
        being who `ThresholdPolicy` routes that AI's own proposals to for
        review (SPEC §7.4/ADR-0003); only actual authorship/delegation
        disqualifies a resolver, not the owner relationship.
        Resolution is recorded on the contradiction and appears in provenance.

        Args:
            contradiction_id: ID of the contradiction to resolve
            winner_assertion_id: ID of the member assertion to keep active
            resolver: Principal ID resolving the contradiction

        Returns:
            Updated Contradiction with state `resolved`

        Raises:
            AuthError: resolver is not a known principal
            NotFoundError: contradiction_id does not name an existing
                contradiction, or a member assertion could not be found
                (assertions are append-only and never deleted, so this
                indicates data corruption, not a benign gap)
            CapabilityError: resolver lacks review/admin capability, is
                AI-kind, or is the author/delegate of any member assertion
            ValidationError: contradiction is not open, or winner_assertion_id
                is not one of its members
        """
        resolver_principal = self.backend.get_principal(resolver)
        if resolver_principal is None:
            raise AuthError(f"Principal not found: {resolver}")
        if resolver_principal.default_capability not in ("review", "admin"):
            raise CapabilityError(f"Principal {resolver} lacks review capability")
        if resolver_principal.kind == "ai":
            raise CapabilityError(f"Principal {resolver!r} is an AI principal and cannot review")

        # Contradiction/membership/self-resolution validation runs inside the
        # transaction, not before it: reading contradiction.member_ids
        # outside the transaction would let another thread extend the same
        # open contradiction (e.g. via a concurrent propose()) between the
        # validation and the write below — a new member would then escape
        # both the self-resolution check and the retraction loop entirely.
        # Raising here rolls back a no-op (nothing has been written yet).
        now = self.clock.now()
        with self.backend.transaction():
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
            for member_id in contradiction.member_ids:
                member = self.backend.get_assertion(member_id)
                if member is None:
                    # Assertions are append-only and never deleted (SPEC
                    # §5) — a contradiction member that can't be found is
                    # data corruption, not a benign gap to skip past.
                    raise NotFoundError(
                        f"Assertion {member_id!r}, a member of contradiction "
                        f"{contradiction_id!r}, could not be found"
                    )
                if resolver in (member.author, member.acting_as):
                    raise CapabilityError(
                        f"Principal {resolver!r} cannot resolve a contradiction they are "
                        f"party to (author or delegate of member assertion {member_id!r})"
                    )

            for member_id in contradiction.member_ids:
                if member_id != winner_assertion_id:
                    self.backend.set_assertion_status(
                        member_id, "retracted", valid_to=self._retraction_valid_to(member_id, now)
                    )
                    self._record_assertion_event(member_id, resolver, "retracted", now)
            self.backend.set_assertion_status(winner_assertion_id, "active")
            self._record_assertion_event(winner_assertion_id, resolver, "reactivated", now)
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

        # get_assertion is status-agnostic: a flagged/superseded assertion
        # must still be resolvable here, e.g. when extending an open contradiction
        a = self.backend.get_assertion(assertion_id_a)
        b = self.backend.get_assertion(assertion_id_b)
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

        now = self.clock.now()
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
                        created_at=now,
                        raised_by=author,
                        metadata={"rationale": rationale} if rationale else {},
                    )
                )
                action = "created"

            for aid, assertion in ((assertion_id_a, a), (assertion_id_b, b)):
                if assertion.status != "flagged":
                    self.backend.set_assertion_status(aid, "flagged")
                    self._record_assertion_event(aid, author, "flagged", now)

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
        self.require_admin(author)

        current = self.backend.get_schema(schema.namespace)
        expected_version = (current.version + 1) if current is not None else 1
        if schema.version != expected_version:
            raise SchemaError(
                f"Schema version {schema.version} is not the next monotonic version "
                f"for namespace {schema.namespace!r} (expected {expected_version})"
            )

        self.backend.put_schema(schema)
        return schema

    def require_admin(self, author: str) -> Principal:
        """Shared gate for admin-level actions (credential issuance/revocation,
        plugin registration): these convert local access into a remote,
        network-reachable capability or a standing in-process actor, so they
        require `admin`, not just whatever capability the target has."""
        principal = self.backend.get_principal(author)
        if principal is None:
            raise AuthError(f"Principal not found: {author}")
        if principal.default_capability != "admin":
            raise CapabilityError(f"Principal {author} lacks admin capability")
        return principal

    def list_principals(self, author: str) -> list[Principal]:
        """List all principals (KI-022).

        Args:
            author: Principal ID performing the lookup — must hold `admin`
                capability (principal metadata, including `owner` and
                `trust_level`, is admin-tier information, same sensitivity
                class as credential listing)

        Returns:
            All principals, most recently created first

        Raises:
            AuthError: author does not name an existing principal
            CapabilityError: author lacks admin capability
        """
        self.require_admin(author)
        return self.backend.list_principals()

    def issue_token(self, principal_id: str, author: str) -> tuple[str, str]:
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
            author: Principal ID performing the issuance — must hold `admin`
                capability (issuing a token mints a remote credential, a
                higher-stakes action than the target principal's own
                capability level)

        Returns:
            A ``(token, credential_id)`` tuple. ``token`` is the raw secret
            (not persisted anywhere — save it now). ``credential_id`` is
            returned directly rather than needing to be re-derived via
            ``list_tokens(...)[0]`` (KI-024) — that second, non-transactional
            lookup could race a concurrent issuance for the same principal
            and return a different credential's id.

        Raises:
            AuthError: author or principal_id does not name an existing principal
            CapabilityError: author lacks admin capability
        """
        import secrets

        from ontolith.identity.token_auth import hash_token

        self.require_admin(author)
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
        return raw_token, credential.id

    def revoke_token(self, credential_id: str, author: str) -> None:
        """Revoke a previously issued token by its credential ID (ADR-0014).

        Args:
            credential_id: Credential to revoke (returned alongside the raw
                token by a token-issuance CLI/tool, not the token itself)
            author: Principal ID performing the revocation — must hold
                `admin` capability

        Raises:
            AuthError: author does not name an existing principal
            CapabilityError: author lacks admin capability
            NotFoundError: No credential with that ID exists
        """
        self.require_admin(author)
        credential = self.backend.get_credential(credential_id)
        if credential is None:
            raise NotFoundError(f"Token credential not found: {credential_id}")
        self.backend.revoke_credential(credential_id, self.clock.now())

    def list_tokens(self, principal_id: str, author: str) -> list[PrincipalCredential]:
        """List all credentials (active and revoked) issued to a principal.

        Never returns the raw token or its hash — only id/created_at/revoked_at,
        enough to identify which credential to pass to ``revoke_token``.

        Args:
            principal_id: Principal to list credentials for
            author: Principal ID performing the lookup — must hold `admin`
                capability

        Returns:
            Credentials for this principal, most recently issued first

        Raises:
            AuthError: author does not name an existing principal
            CapabilityError: author lacks admin capability
        """
        self.require_admin(author)
        return self.backend.get_credentials_for_principal(principal_id)

    def close(self) -> None:
        """Close the knowledge base connection."""
        self.backend.close()


__all__ = ["AsOfView", "Ontology"]
