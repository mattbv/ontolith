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
from typing import Any, Protocol

from ontolith.core import Assertion, AssertionEvent, Entity, Namespace
from ontolith.govern.contradiction import Contradiction
from ontolith.govern.proposal import Proposal, ProposalEvent
from ontolith.identity import AdminEvent, Principal, PrincipalCredential
from ontolith.schema import SchemaIR

VECTOR_SCOPES = frozenset({"entity", "assertion"})
"""Closed set of embedding scopes (SPEC §11.3): entity- and assertion-level.

Deliberately not an arbitrary caller-supplied string — both backends use
`scope` to name per-scope storage (SQLite: a vec0 virtual table per scope;
DuckDB: a plain table per scope), so an open string would mean dynamic DDL
driven by caller input. vector_upsert/vector_search MUST reject any scope
outside this set with ValidationError.
"""

DEFAULT_NAMESPACE = "default"
"""The one namespace this project operates in today (KI-022).

Ontolith is still single-namespace throughout (ADR-0015) — `Ontology`
always writes to this namespace, and both backends seed a matching
`namespace` registry row for it at schema-creation time. Shared by
`Ontology` and both backends so that specific trio stays in sync by
construction; a handful of other unrelated `"default"` literals elsewhere
(e.g. REST/MCP route defaults, example scripts) are independent naming
choices, not instances of this constant, and aren't required to match it.
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
        """Begin a new transaction.

        This port makes no promise about *when* a concurrent writer is
        serialized against this one (at `begin()` versus at the first
        conflicting statement) — that's a backend-specific locking detail,
        not a cross-backend contract. See `SQLiteBackend.begin()`'s own
        docstring (KI-084) for the default backend's specific choice and
        why it matters for cross-process write safety.
        """
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

    def list_principals(self) -> list[Principal]:
        """List all principals (KI-022).

        Returns:
            All principals, most recently created first
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

    def revoke_credential(self, credential_id: str, revoked_at: datetime, revoked_by: str) -> None:
        """Mark a credential as revoked.

        Args:
            credential_id: Credential to revoke
            revoked_at: Timestamp of revocation
            revoked_by: Principal ID of the admin performing the
                revocation (KI-060)

        Raises:
            StorageError: If the credential is not found
        """
        ...

    def put_admin_event(self, event: AdminEvent) -> None:
        """Persist an append-only admin-action event (KI-060, SPEC §17).

        Args:
            event: AdminEvent to persist

        Raises:
            StorageError: If persistence fails
        """
        ...

    def get_admin_events(
        self, actor: str | None = None, target: str | None = None
    ) -> list[AdminEvent]:
        """Retrieve admin events, optionally filtered by actor or target.

        Args:
            actor: Filter to events performed by this principal ID
            target: Filter to events against this target

        Returns:
            Matching events, oldest first
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

    def get_entity_by_natural_key(
        self, namespace: str, concept: str, natural_key: str
    ) -> Entity | None:
        """Retrieve an entity by its unique `(namespace, concept, natural_key)`
        triple (KI-091) — the same uniqueness the `entity` table's own
        `UNIQUE(namespace, concept, natural_key)` constraint enforces, used
        to pre-check a conflict before `put_entity` rather than surfacing
        one late as a redacted `StorageError`.

        Args:
            namespace: Namespace to search within
            concept: Concept name
            natural_key: Natural key to look up

        Returns:
            Entity if one with this exact triple exists, None otherwise
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

    def list_namespaces(self) -> list[Namespace]:
        """List all registered namespaces (SPEC §12.2, KI-022).

        Returns:
            All namespaces, most recently created first
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

    def get_schema_at(self, namespace: str, at: datetime) -> SchemaIR | None:
        """Retrieve the schema version effective at a point in time (KI-019).

        Resolves the highest version whose `applied_at <= at` — i.e. the
        schema that was current at time `at`, for bitemporal reconstruction
        (SPEC §11.4: "Schema is resolved to the schema_version effective at
        t"). `put_schema` already records `applied_at` via the backend's
        injected Clock; this method is the first reader of that column.

        Args:
            namespace: Namespace to query
            at: Point in time to resolve the effective schema for

        Returns:
            Schema effective at `at`, or None if no version had been applied
            by that time (including when the namespace has no schema at all,
            or its first version postdates `at`)
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
        predicate_filters: list[tuple[str, str, Any]],
        as_of_time: datetime | None = None,
        include_flagged: bool = False,
        include_history: bool = False,
    ) -> list[Entity]:
        """Query entities matching all predicate filters in one SQL query.

        Avoids the N+1 pattern of entities() + per-entity assertions() calls.
        Each filter is `(full_predicate, operator, value)`; ALL must match
        (AND semantics) — a list, not a dict, since two different operators
        can target the same predicate (e.g. an `age` range needs both a
        `"gte"` and a `"lt"` filter). `operator` is one of:

        - `"eq"`: equality. Matches either a literal property (`value_lit`)
          or a relation's target entity id (`value_ref`) — KI-030.
        - `"contains"`: case-sensitive substring match against `value_lit`
          only (KI-039) — relations have no defined substring semantics, so
          this operator never matches against `value_ref`. Case-sensitive
          on both backends by construction: SQLite's `LIKE` is
          case-insensitive by default and DuckDB's is not, so
          `SQLiteBackend` explicitly sets `PRAGMA case_sensitive_like = ON`
          at connection time to make the two agree — a conformant
          third-party backend implementing this port must match that
          behavior, not SQLite's un-pragma'd default.
        - `"gt"`/`"lt"`/`"gte"`/`"lte"`: numeric range against `value_lit`
          only (KI-039) — a numeric cast (`CAST`/`TRY_CAST`, exact type
          backend-specific — see e.g. `DuckDBBackend`'s docstring for why
          `DOUBLE` not `REAL`). Callers (in practice, only `QueryBuilder`)
          are responsible for restricting this to predicates *declared*
          numeric — see `_RANGE_VALUE_TYPES` in `ontolith.query.builder`'s
          docstring for why the backend itself doesn't validate that. A
          backend is NOT responsible for validating that already-stored
          `value_lit` content actually parses as a number for a predicate
          declared numeric (KI-049) — implementations should fail safe
          (exclude the row) rather than raise for a value that doesn't
          parse, the way `TRY_CAST` does; letting a raw conversion
          exception escape through this port violates SPEC §16's error
          taxonomy.

        Args:
            namespace: Namespace to query
            concept: Concept to filter by
            predicate_filters: List of `(full_predicate, operator, value)`
                triples (KI-039)
            as_of_time: If set, applies bitemporal filter on assertions and entity creation
            include_flagged: Whether to also match 'flagged' assertions
                (KI-081). Honored on both the current-state and the
                as_of_time path (excluded by default on both).
            include_history: Whether to also match 'superseded' and
                'retracted' assertions (KI-081). On the current-state path
                this widens beyond 'active'. On the as_of_time path,
                'superseded' is unaffected either way (its window already
                never restricts to 'active') — but 'retracted' does
                something under this flag now (ADR-0049, KI-095): the
                as_of_time branch additionally excludes a 'retracted'
                assertion once its own retraction event's timestamp is
                <= as_of_time; this parameter opts back out of that
                exclusion.

        Returns:
            List of entities where all filters match at the given time
        """
        ...

    def entities_meeting_confidence(
        self,
        namespace: str,
        concept: str,
        threshold: float,
        as_of_time: datetime | None = None,
        candidate_ids: frozenset[str] | None = None,
        include_flagged: bool = False,
        include_history: bool = False,
    ) -> set[str]:
        """IDs of entities in `(namespace, concept)` with >=1 assertion at or
        above `threshold` confidence, active at `as_of_time` (or currently
        active, if `as_of_time` is None).

        Avoids the N+1 pattern of calling assertions() once per candidate
        entity (QueryBuilder.min_confidence(), KI-028) — one SQL round trip
        regardless of concept size. `None` confidence never qualifies
        (ADR-0004). When `as_of_time` is None, "active" means
        `status = 'active'`, widened by `include_flagged`/`include_history`
        exactly as `entities_where()`'s current-state path is (KI-093); when
        `as_of_time` is set, it means the same bitemporal window
        `entities_where()` uses (`asserted_at <= as_of_time`, `valid_from`/
        `valid_to` bracketing `as_of_time`) — KI-036, so
        `kb.as_of(t).query(...).min_confidence(...)` evaluates against a
        coherent point-in-time view instead of always checking
        current-active assertions regardless of `t`. One caveat: `status`
        itself is not bitemporally versioned, only current status is ever
        stored, so a `status = 'flagged'` assertion (a static contradiction,
        SPEC §10.3) is excluded even at a `t` before it was flagged, unless
        `include_flagged` is set — matching `entities_where()`'s identical
        `as_of` handling. `include_history` is mostly a no-op under
        `as_of_time`, also matching `entities_where()`: that branch never
        restricts to `active` in the first place, only conditionally
        excludes `flagged`, so a `superseded` assertion whose window covers
        `t` already qualifies without this flag. A `retracted` assertion is
        the one exception (ADR-0049, KI-095): `as_of_time` additionally
        excludes it once its own retraction event's timestamp is <=
        `as_of_time`, and `include_history` opts back out of that
        exclusion.

        Always scoped by `(namespace, concept)` — this is what keeps the
        query's parameter count constant regardless of how many entities
        exist, unlike a mandatory id-list-bound design (a SQL `IN (...)`
        with one placeholder per candidate hits both SQLite's
        bound-variable limit and, on DuckDB, per-parameter bind overhead,
        at real-world scale — see KI-028's own Fix text for why that design
        was tried and reverted before this method first shipped).
        `candidate_ids`, when given, is an *optional* narrowing hint on top
        of that scope — not a replacement for it — for when the caller has
        already narrowed to a small candidate set via `.where()`/
        `.semantic()` (KI-028's fix otherwise forced even a single-candidate
        `.where()` match to re-scan the entire concept; KI-037). A backend
        MAY use it to cut real work (e.g. SQLite binds it as one
        JSON-encoded parameter, avoiding the per-placeholder cost a literal
        `IN (...)` would reintroduce) or ignore it and keep scanning — both
        are correct, since the caller always re-intersects the returned set
        against its own candidate list.

        Args:
            namespace: Namespace to scope the scan to
            concept: Concept to scope the scan to
            threshold: Minimum confidence, 0.0-1.0
            as_of_time: If set, evaluate against this point in time instead
                of current state (KI-036) — see the flagged-status caveat
                above
            candidate_ids: Optional narrowing hint (KI-037) — a backend may
                use this to scope the scan below `(namespace, concept)`,
                but is not required to
            include_flagged: Also count 'flagged' assertions (KI-093).
                Honored on both the current-state and as_of_time paths,
                matching entities_where().
            include_history: Also count 'superseded'/'retracted' assertions
                (KI-093). Widens the current-state path beyond 'active'.
                Under as_of_time, mostly a no-op — but not for 'retracted'
                (ADR-0049, KI-095), matching entities_where().

        Returns:
            IDs of qualifying entities (may be a superset of any candidate
            list the caller intends to intersect this against)
        """
        ...

    def entities_meeting_trust(
        self,
        namespace: str,
        concept: str,
        min_trust: int,
        as_of_time: datetime | None = None,
        candidate_ids: frozenset[str] | None = None,
        include_flagged: bool = False,
        include_history: bool = False,
    ) -> set[str]:
        """IDs of entities in `(namespace, concept)` with >=1 assertion,
        active at `as_of_time` (or currently active, if `as_of_time` is
        None) — widened by `include_flagged`/`include_history` exactly as
        `entities_meeting_confidence()`'s identical parameters are (KI-093)
        — whose *effective* trust_level >= `min_trust`.

        "Effective" (KI-047): when the qualifying assertion was made under
        delegation (`acting_as` set), the comparison is
        `min(author.trust_level, acting_as.trust_level)`, not the author's
        raw `trust_level` alone. SPEC §8.4 states this `min()` rule for
        *capability* only ("the effective capability for the operation is
        `min(capability(author), capability(acting_as))`"); the trust-min
        is `govern/policy.py`'s own conservative extension of that same
        principle, applied here by analogy, not a separate SPEC mandate.
        For a non-delegated assertion, this is simply the author's own
        `trust_level`, unchanged from before this method considered
        delegation at all.

        If `acting_as` names a principal that doesn't resolve (there is no
        FK from `assertion.acting_as` to `principal.id`, so this can only
        happen via a direct `put_assertion()` call bypassing `Ontology`'s
        write paths — e.g. a legacy import — since `Ontology`'s own paths
        always validate the delegate exists before writing), implementations
        MUST fall back to the author's own `trust_level` rather than
        excluding the row or raising — i.e. treat an unresolvable delegate
        the same as no delegate at all. This deliberately fails *open*,
        unlike `govern/policy.py`'s `_resolve_delegation` which fails
        *closed* (raises `AuthError`) for the same input — policy
        evaluation runs once, at write time, when rejecting the write
        outright is cheap and correct; this method runs on every query
        against already-committed data, where excluding or erroring on a
        row for data that was already accepted would be a surprising,
        un-auditable behavior change with no corresponding write.

        Avoids the N+1 pattern of calling assertions() + get_principal()
        once per (candidate entity, assertion) pair (QueryBuilder.
        trust_at_least(), KI-028) — one SQL round trip regardless of
        concept size. Scoped by `(namespace, concept)`, with the same
        optional `candidate_ids` narrowing hint (KI-037), for the same
        reason as `entities_meeting_confidence` — see its docstring.

        `as_of_time` bitemporally scopes which *assertion* qualifies, the
        same way `entities_meeting_confidence` does (including its
        flagged-status caveat) — but each individual principal's own
        `trust_level` (author's and, if delegated, `acting_as`'s) is always
        its current value, never a historical one (KI-036), and the `min()`
        this method now takes of the two (KI-047) inherits that same
        property. This is not an approximation: no code path updates a
        principal's `trust_level` after creation, so "trust_level as of any
        t at or after the principal's creation" and "trust_level now" are
        the same value by construction (guarded by
        `tests/unit/test_principal_trust_immutability_invariant.py`, which
        fails the day a mutation path is added — that would mean this
        method needs real principal versioning, not this shortcut). A
        principal cannot author an assertion before it exists, so this
        holds for every `as_of_time` an assertion's `asserted_at` could
        satisfy.

        Args:
            namespace: Namespace to scope the scan to
            concept: Concept to scope the scan to
            min_trust: Minimum effective trust level, 0-10
            as_of_time: If set, evaluate assertion existence against this
                point in time instead of current state (KI-036)
            candidate_ids: Optional narrowing hint (KI-037) — see
                `entities_meeting_confidence`'s docstring
            include_flagged: Also count 'flagged' assertions (KI-093) — see
                `entities_meeting_confidence`'s docstring
            include_history: Also count 'superseded'/'retracted' assertions
                (KI-093) — see `entities_meeting_confidence`'s docstring

        Returns:
            IDs of qualifying entities (may be a superset of any candidate
            list the caller intends to intersect this against)
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
            decided_at: ISO timestamp of the decision. Set unconditionally,
                including to None — unlike policy_reason, passing None
                clears the stored value rather than leaving it unchanged.
                `Ontology.resubmit` (KI-027) relies on this to re-open an
                already-decided proposal: a resubmission that lands back in
                require_review is not yet decided again, and must clear the
                prior decided_at rather than keep the stale value from the
                request_changes decision it's superseding.
            policy_reason: Human-readable reason from policy engine. If None,
                the stored value is left unchanged (not cleared) — the
                policy-engine reason set at proposal-creation time is
                distinct from, and not overwritten by, review actions
                recorded via put_proposal_event.

        Raises:
            StorageError: proposal_id does not name an existing proposal
        """
        ...

    def update_proposal_reviewers(self, proposal_id: str, reviewers: list[str]) -> None:
        """Replace a proposal's assigned reviewers (SPEC §9.4's `assign` action).

        Unlike `update_proposal_state`'s `policy_reason`, there is no
        "leave unchanged" sentinel here — `reviewers` is always replaced
        wholesale with what's passed, including an empty list (which
        clears every assignment). A dedicated method rather than folding
        this into `update_proposal_state`: `assign` doesn't change
        `state`, and `update_proposal_state` already has enough
        state/decided_at/policy_reason parameters with their own distinct
        semantics without adding a fourth (KI-078).

        Args:
            proposal_id: Proposal to update
            reviewers: New reviewer list, replacing whatever was there before

        Raises:
            StorageError: proposal_id does not name an existing proposal
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
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Replace the member_ids list (and, optionally, the metadata blob)
        on an existing contradiction (KI-071).

        Args:
            contradiction_id: Contradiction to update
            member_ids: New full list of member assertion IDs
            metadata: If given, replaces the contradiction's metadata blob
                wholesale — the caller is expected to pass the complete
                desired dict (e.g. built from a fresh read plus one
                appended entry), matching member_ids' own
                full-replacement convention rather than a merge/delta.
                ``None`` (the default) leaves metadata untouched.
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
