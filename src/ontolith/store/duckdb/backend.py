"""DuckDB storage backend implementation.

Second StorageBackend adapter (M3, ADR-0016). Provides:
- Append-only entity and assertion storage
- Transaction management
- Query interface

Structurally parallel to SQLiteBackend (src/ontolith/store/sqlite/backend.py)
but diverges where DuckDB's Python client semantics require it — see inline
notes at each divergence point:
- Autocommit is DuckDB's default outside an explicit BEGIN; unlike sqlite3's
  autocommit mode, issuing COMMIT/ROLLBACK with no open transaction raises
  duckdb.TransactionException rather than no-op'ing. Per-write methods
  therefore never call commit()/rollback() themselves.
- DuckDB's cursor.rowcount is always -1 after UPDATE (confirmed empirically,
  duckdb==1.5.4), so "row not found" detection can't use sqlite3's rowcount
  check. Where the target table has no incoming foreign key from another
  table (contradiction, principal_credential), `UPDATE ... RETURNING id`
  substitutes cleanly. assertion (referenced by assertion_event.assertion_id)
  and proposal (referenced by proposal_event.proposal_id) instead use a
  SELECT existence check followed by a plain UPDATE — RETURNING against
  either raises a false-positive constraint violation unconditionally, and
  even a plain UPDATE raises the same false-positive once the full schema
  (this project's actual indexes/CHECK constraints, not a minimal repro) is
  in play and a child row already references the target (confirmed
  empirically against the real schema in both forms, a documented DuckDB
  limitation). Dropping these two FK declarations entirely — not just
  avoiding RETURNING — is what actually fixes it; see the comments on each
  table for the reproduction. Referential integrity for both links is
  enforced at the application layer instead (Ontology only ever calls
  put_assertion_event/put_proposal_event with a real, just-persisted id).
- DuckDB rows are plain tuples, not dict-like sqlite3.Row objects; row-mapping
  goes through a small _row_to_dict() helper keyed off cursor.description.
- `confidence` is declared DOUBLE, not REAL: DuckDB's REAL is 4-byte single
  precision (unlike SQLite's REAL, always 8-byte double), which silently
  rounds values like 0.95.
- The `at` column (assertion_event, proposal_event) is quoted ("at") in DDL
  and SQL — it's a reserved word in DuckDB, unlike SQLite.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from ontolith.core import Assertion, AssertionEvent, Clock, Entity, Namespace, SystemClock
from ontolith.core.errors import StorageError, ValidationError
from ontolith.govern.contradiction import Contradiction
from ontolith.govern.proposal import Proposal, ProposalEvent
from ontolith.identity import Principal, PrincipalCredential
from ontolith.schema import SchemaIR
from ontolith.store.base import DEFAULT_NAMESPACE, VECTOR_SCOPES

_RANGE_SQL_OPERATORS = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
"""entities_where() operator name -> SQL comparison operator (KI-039)."""


def _like_escape(value: str) -> str:
    """Escape SQL LIKE wildcards so a `__contains` filter matches `value`
    literally, not as a LIKE pattern (KI-039). Paired with `ESCAPE '\\'` in
    the SQL and the value wrapped in `%...%` by the caller."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class DuckDBBackend:
    """DuckDB implementation of StorageBackend.

    Schema follows SPEC §12.2, identical table shape to SQLiteBackend:
    - entity table with ULID primary key
    - assertion table with ULID primary key
    - Bitemporal columns (asserted_at, valid_from, valid_to)
    - Status tracking for append-only invariant
    """

    def __init__(self, path: str | Path, *, clock: Clock | None = None) -> None:
        """Initialize DuckDB backend.

        Args:
            path: Path to DuckDB database file (created if doesn't exist)
            clock: Clock for timestamps (defaults to SystemClock)
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.path))
        self._clock: Clock = clock or SystemClock()
        self._create_schema()

    def _create_schema(self) -> None:
        """Create database schema if not exists."""
        # Principal table (SPEC §8, ADR-0003, ADR-0009)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS principal (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('human', 'ai', 'service')),
                owner TEXT,
                auth_method TEXT NOT NULL CHECK(auth_method IN ('oidc', 'workload', 'apikey')),
                default_capability TEXT NOT NULL DEFAULT 'propose' CHECK(default_capability IN ('read', 'propose', 'write', 'review', 'admin')),
                trust_level INTEGER NOT NULL DEFAULT 0 CHECK(trust_level BETWEEN 0 AND 10),
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                CHECK (kind <> 'ai' OR owner IS NOT NULL),
                FOREIGN KEY(owner) REFERENCES principal(id)
            )
        """)

        # Principal credential table (ADR-0014) — hashed API-key tokens.
        # The raw token is never persisted, only its SHA-256 hash. A principal
        # may hold multiple concurrent active credentials (rotation = issue
        # new + revoke old, both explicit); revoked rows are kept, not deleted.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS principal_credential (
                id TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                FOREIGN KEY(principal_id) REFERENCES principal(id)
            )
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_principal_credential_principal
            ON principal_credential(principal_id)
        """)

        # Namespace registry table (SPEC §12.2, KI-022) — tracks namespaces
        # that have a schema applied or are the seeded default; NOT a
        # complete registry of every namespace string ever written to an
        # entity/assertion row (those remain free-text, unvalidated against
        # this table — see ADR-0022's Update section for the deliberate
        # scope boundary). `metadata TEXT NOT NULL DEFAULT '{}'` deviates
        # from SPEC §12.2's literal nullable `metadata TEXT` — matches this
        # project's `principal` table convention and guarantees
        # `_row_to_namespace`'s `json.loads` never sees NULL.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS namespace (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """)
        self._ensure_namespace_registered(DEFAULT_NAMESPACE)

        # Schema version table (SPEC §12.2, §6.4)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                namespace TEXT NOT NULL,
                version INTEGER NOT NULL,
                definition TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                PRIMARY KEY (namespace, version)
            )
        """)

        # Entity table (SPEC §12.2)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS entity (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                concept TEXT NOT NULL,
                natural_key TEXT,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                UNIQUE(namespace, concept, natural_key),
                FOREIGN KEY(created_by) REFERENCES principal(id)
            )
        """)

        # Assertion table (SPEC §12.2)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS assertion (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                value_kind TEXT NOT NULL CHECK(value_kind IN ('literal', 'ref')),
                value_type TEXT,
                value_lit TEXT,
                value_ref TEXT,
                author TEXT NOT NULL,
                acting_as TEXT,
                source TEXT,
                -- DOUBLE not REAL: DuckDB's REAL is 4-byte single precision,
                -- unlike SQLite's REAL which is always 8-byte double
                -- precision. REAL here silently rounds e.g. 0.95 to
                -- 0.9499999... (confirmed empirically) — DOUBLE matches
                -- SQLiteBackend's actual behavior.
                confidence DOUBLE CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
                rationale TEXT,
                model TEXT,
                asserted_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'retracted', 'flagged')),
                proposal_id TEXT,
                supersedes TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(subject) REFERENCES entity(id),
                FOREIGN KEY(author) REFERENCES principal(id)
            )
        """)

        # Assertion event table — append-only audit log for status
        # mutations (supersession, flagging, retraction, reactivation).
        # The assertion row itself only carries current status; this table
        # makes each transition independently attributable and timestamped.
        #
        # No FOREIGN KEY(assertion_id) REFERENCES assertion(id): with the full
        # schema below (this table's own CHECK/indexes included, not a
        # trimmed-down repro), DuckDB 1.5.4 spuriously raises a constraint
        # violation on a later UPDATE to assertion.status once a child
        # assertion_event row already references it — reproduced against the
        # real DuckDBBackend-created schema with both `UPDATE ... RETURNING`
        # and a plain `UPDATE` (see set_assertion_status). That exact sequence
        # (mutate the assertion's status again after its own audit trail
        # already has an entry) is this table's whole reason to exist, so the
        # FK can't be kept without breaking normal appends. Referential
        # integrity for assertion_id is enforced at the application layer
        # instead (Ontology only ever calls put_assertion_event with a real,
        # just-persisted assertion id) — SQLiteBackend keeps the FK since
        # sqlite3 doesn't have this limitation.
        #
        # successor_id (KI-008) has no FOREIGN KEY(successor_id) REFERENCES
        # assertion(id) either, for the same reason: _apply_with_conflict_
        # routing persists the successor assertion first and then updates
        # each predecessor's status in the same transaction as these event
        # inserts, which is exactly the mutate-after-referenced sequence
        # above — an FK here would risk the identical spurious violation.
        # Referential integrity is enforced at the application layer, same
        # as assertion_id. SQLiteBackend keeps the FK for this column too.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS assertion_event (
                id TEXT PRIMARY KEY,
                assertion_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('superseded', 'flagged', 'retracted', 'reactivated')),
                "at" TEXT NOT NULL,
                successor_id TEXT,
                FOREIGN KEY(actor) REFERENCES principal(id)
            )
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_event_assertion
            ON assertion_event(assertion_id)
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_event_successor
            ON assertion_event(successor_id)
        """)

        # Proposal table (SPEC §9.1)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS proposal (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                author TEXT NOT NULL,
                acting_as TEXT,
                state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN (
                    'draft', 'submitted', 'auto_accepted',
                    'require_review', 'under_review',
                    'accepted', 'rejected', 'changes_requested'
                )),
                created_at TEXT NOT NULL,
                decided_at TEXT,
                policy_reason TEXT,
                payload TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(author) REFERENCES principal(id)
            )
        """)

        # Proposal event table (SPEC §9.4) — structured review actions.
        # Scoped to accept/reject/request_changes, the three review actions
        # that exist as Ontology methods; assign/comment are not implemented
        # yet (see ProposalEvent docstring).
        #
        # No CHECK on `type` — matches SPEC §12.2's own DDL and this
        # project's "validate at edges, trust within" boundary (Pydantic's
        # `ProposalEvent.type` Literal already enforces the vocabulary at
        # construction time); see the SQLite backend's identical comment for
        # the migration-hazard reasoning (`CREATE TABLE IF NOT EXISTS` never
        # widens an already-created table's constraint).
        #
        # No FOREIGN KEY(proposal_id) REFERENCES proposal(id): same DuckDB
        # limitation as assertion_event.assertion_id above — proposal rows
        # are updated (update_proposal_state) after proposal_event rows may
        # already reference them, which DuckDB's FK enforcement mishandles.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS proposal_event (
                id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                type TEXT NOT NULL,
                detail TEXT,
                "at" TEXT NOT NULL,
                FOREIGN KEY(actor) REFERENCES principal(id)
            )
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_proposal_event_proposal
            ON proposal_event(proposal_id)
        """)

        # Contradiction table (SPEC §10.3)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS contradiction (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open', 'resolved')),
                member_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                raised_by TEXT,
                resolved_by TEXT,
                resolved_at TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_contradiction_open
            ON contradiction(namespace, subject, predicate, state)
        """)

        # Indexes (SPEC §12.2)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_spo
            ON assertion(namespace, subject, predicate, status)
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_subj
            ON assertion(namespace, subject)
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_time
            ON assertion(asserted_at)
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_valid
            ON assertion(valid_from, valid_to)
        """)

        # idx_assertion_spo above leads with namespace, which every query
        # leaves unconstrained (single-namespace today), making it unusable
        # for the actual filter shapes in assertions()/entities_where() —
        # confirmed via EXPLAIN (full table scan, not index search). These
        # two match the real WHERE clauses without requiring namespace.
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_subj_pred_status
            ON assertion(subject, predicate, status)
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_pred_value
            ON assertion(predicate, value_lit, status)
        """)

        # Companion to idx_assertion_pred_value for relation (value_ref)
        # filters in entities_where() (KI-030) — kept as a separate index,
        # matching the SQLite backend, rather than an OR-shaped predicate
        # against a single combined index.
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_pred_ref
            ON assertion(predicate, value_ref, status)
        """)

        # Vector storage bookkeeping (SPEC §11.3, ADR-0020). One plain table
        # per scope (`vector_entity`, `vector_assertion`), created lazily on
        # first vector_upsert once the scope's dimension is known — see
        # _ensure_vector_table. Unlike SQLite's vec0 tables, DuckDB's plain
        # TEXT-PRIMARY-KEY table supports INSERT OR REPLACE directly
        # (verified empirically), so no rowid-indirection table is needed
        # here the way SQLiteBackend requires.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS vector_scope (
                scope TEXT PRIMARY KEY,
                dim INTEGER NOT NULL
            )
        """)

        # Note: DuckDB's optimizer does not use secondary ART indexes for
        # this equality-filter shape (confirmed via EXPLAIN at 20k+ rows —
        # always SEQ_SCAN, unlike SQLite which switches to an index SEARCH
        # with the equivalent indexes above). Its vectorized scan is still
        # within budget regardless (measured ~3.4ms/call at 100k assertions
        # vs SQLite's ~0.14ms indexed and ~7ms pre-fix scanned), so these
        # indexes are kept for schema parity with SQLiteBackend and in case
        # a future DuckDB version leverages them, not because they currently
        # change this backend's query plan.

    @staticmethod
    def _row_to_dict(cursor: duckdb.DuckDBPyConnection, row: tuple[Any, ...]) -> dict[str, Any]:
        """Zip a positional result row with its cursor's column names."""
        columns = [d[0] for d in cursor.description]
        return dict(zip(columns, row, strict=True))

    def begin(self) -> None:
        """Begin an explicit transaction."""
        self.conn.execute("BEGIN TRANSACTION")

    def commit(self) -> None:
        """Commit the current explicit transaction."""
        try:
            self.conn.execute("COMMIT")
        except duckdb.Error as e:
            raise StorageError(f"Failed to commit transaction: {e}") from e

    def rollback(self) -> None:
        """Rollback the current explicit transaction."""
        try:
            self.conn.execute("ROLLBACK")
        except duckdb.Error as e:
            raise StorageError(f"Failed to rollback transaction: {e}") from e

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Context manager for atomic multi-write transactions.

        Usage:
            with backend.transaction():
                backend.put_entity(entity)
                backend.put_assertion(assertion)
        """
        self.begin()
        try:
            yield
            self.commit()
        except Exception:
            self.rollback()
            raise

    def put_principal(self, principal: Principal) -> None:
        """Persist a principal.

        Args:
            principal: Principal to persist

        Raises:
            StorageError: If persistence fails
        """
        try:
            self.conn.execute(
                """
                INSERT INTO principal (id, kind, owner, auth_method, default_capability, trust_level, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    principal.id,
                    principal.kind,
                    principal.owner,
                    principal.auth_method,
                    principal.default_capability,
                    principal.trust_level,
                    principal.created_at.isoformat(),
                    json.dumps(principal.metadata),
                ],
            )
        except duckdb.IntegrityError as e:
            raise StorageError(f"Principal conflict (id={principal.id}): {e}") from e
        except duckdb.Error as e:
            raise StorageError(f"Failed to persist principal (id={principal.id}): {e}") from e

    def get_principal(self, principal_id: str) -> Principal | None:
        """Retrieve a principal by ID.

        Args:
            principal_id: Principal ID to retrieve

        Returns:
            Principal if found, None otherwise
        """
        cursor = self.conn.execute("SELECT * FROM principal WHERE id = ?", [principal_id])
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_principal(self._row_to_dict(cursor, row))

    def list_principals(self) -> list[Principal]:
        """List all principals (KI-022).

        Returns:
            All principals, most recently created first
        """
        # id DESC is a deterministic lexical tiebreak, not a recency proxy —
        # see SQLiteBackend's identical method for why that distinction
        # matters here (principal ids are user-supplied, unlike credential
        # ids' ULID-based get_credentials_for_principal tiebreak).
        cursor = self.conn.execute("SELECT * FROM principal ORDER BY created_at DESC, id DESC")
        return [self._row_to_principal(self._row_to_dict(cursor, row)) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_principal(row: dict[str, Any]) -> Principal:
        """Deserialize a `principal` table row into a Principal."""
        return Principal(
            id=row["id"],
            kind=row["kind"],
            owner=row["owner"],
            auth_method=row["auth_method"],
            default_capability=row["default_capability"],
            trust_level=row["trust_level"],
            created_at=datetime.fromisoformat(row["created_at"]),
            metadata=json.loads(row["metadata"]),
        )

    def list_namespaces(self) -> list[Namespace]:
        """List all registered namespaces (SPEC §12.2, KI-022).

        Returns:
            All namespaces, most recently created first
        """
        # id DESC tiebreak is purely lexical (namespace ids are slugs, not
        # ULIDs) — same convention as list_principals, not a recency proxy.
        cursor = self.conn.execute("SELECT * FROM namespace ORDER BY created_at DESC, id DESC")
        return [self._row_to_namespace(self._row_to_dict(cursor, row)) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_namespace(row: dict[str, Any]) -> Namespace:
        """Deserialize a `namespace` table row into a Namespace."""
        return Namespace(
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            metadata=json.loads(row["metadata"]),
        )

    def _ensure_namespace_registered(self, namespace: str) -> None:
        """Idempotently register a namespace in the registry, if not already present.

        Read-then-maybe-write rather than an unconditional insert: an
        unconditional insert attempt takes a write lock even when the row
        already exists, which would turn every read-only backend
        construction (e.g. reconnecting just to list namespaces) into a
        blocking write. `ON CONFLICT DO NOTHING` is kept for the write path
        itself, to stay safe against a genuine race between the check and
        the insert.
        """
        existing = self.conn.execute("SELECT 1 FROM namespace WHERE id = ?", [namespace]).fetchone()
        if existing is not None:
            return
        self.conn.execute(
            "INSERT INTO namespace (id, created_at, metadata) VALUES (?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            [namespace, self._clock.now().isoformat(), "{}"],
        )

    def put_credential(self, credential: PrincipalCredential) -> None:
        """Persist a principal credential (hashed API-key token).

        Args:
            credential: PrincipalCredential to persist (token_hash, never the
                raw token)

        Raises:
            StorageError: If persistence fails
        """
        try:
            self.conn.execute(
                """
                INSERT INTO principal_credential
                    (id, principal_id, token_hash, created_at, revoked_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    credential.id,
                    credential.principal_id,
                    credential.token_hash,
                    credential.created_at.isoformat(),
                    credential.revoked_at.isoformat() if credential.revoked_at else None,
                ],
            )
        except duckdb.IntegrityError as e:
            raise StorageError(f"Credential conflict (id={credential.id}): {e}") from e
        except duckdb.Error as e:
            raise StorageError(f"Failed to persist credential (id={credential.id}): {e}") from e

    def get_principal_by_token_hash(self, token_hash: str) -> Principal | None:
        """Resolve a principal via a credential's token hash.

        Only unrevoked credentials resolve. This is the sole read path used
        for MCP authentication — it never trusts a caller-supplied principal
        ID directly.

        Args:
            token_hash: SHA-256 hash of the raw bearer token

        Returns:
            Principal if the hash matches an active (unrevoked) credential,
            None otherwise
        """
        cursor = self.conn.execute(
            """
            SELECT p.* FROM principal p
            JOIN principal_credential c ON c.principal_id = p.id
            WHERE c.token_hash = ? AND c.revoked_at IS NULL
            """,
            [token_hash],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_principal(self._row_to_dict(cursor, row))

    def get_credential(self, credential_id: str) -> PrincipalCredential | None:
        """Retrieve a credential by ID (never exposes the raw token or hash to callers).

        Args:
            credential_id: Credential ID to retrieve

        Returns:
            PrincipalCredential if found, None otherwise
        """
        cursor = self.conn.execute(
            "SELECT * FROM principal_credential WHERE id = ?", [credential_id]
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_credential(self._row_to_dict(cursor, row))

    def get_credentials_for_principal(self, principal_id: str) -> list[PrincipalCredential]:
        """List all credentials (active and revoked) issued to a principal.

        Args:
            principal_id: Principal to list credentials for

        Returns:
            Credentials for this principal, most recently issued first
        """
        # id DESC tiebreaks two credentials issued at the same timestamp —
        # see SQLiteBackend's identical fix for the determinism rationale.
        cursor = self.conn.execute(
            "SELECT * FROM principal_credential WHERE principal_id = ? "
            "ORDER BY created_at DESC, id DESC",
            [principal_id],
        )
        return [
            self._row_to_credential(self._row_to_dict(cursor, row)) for row in cursor.fetchall()
        ]

    @staticmethod
    def _row_to_credential(row: dict[str, Any]) -> PrincipalCredential:
        """Deserialize a `principal_credential` table row into a PrincipalCredential."""
        return PrincipalCredential(
            id=row["id"],
            principal_id=row["principal_id"],
            token_hash=row["token_hash"],
            created_at=datetime.fromisoformat(row["created_at"]),
            revoked_at=datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
        )

    def revoke_credential(self, credential_id: str, revoked_at: datetime) -> None:
        """Mark a credential as revoked. Idempotent-safe: re-revoking is a no-op update.

        Args:
            credential_id: Credential to revoke
            revoked_at: Timestamp of revocation

        Raises:
            StorageError: If the credential is not found
        """
        cursor = self.conn.execute(
            "UPDATE principal_credential SET revoked_at = ? WHERE id = ? RETURNING id",
            [revoked_at.isoformat(), credential_id],
        )
        if not cursor.fetchall():
            raise StorageError(f"Credential not found: {credential_id}")

    def put_entity(self, entity: Entity) -> None:
        """Persist an entity.

        Args:
            entity: Entity to persist

        Raises:
            StorageError: If persistence fails
        """
        try:
            self.conn.execute(
                """
                INSERT INTO entity (id, namespace, concept, natural_key, created_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    entity.id,
                    entity.namespace,
                    entity.concept,
                    entity.natural_key,
                    entity.created_at.isoformat(),
                    entity.created_by,
                ],
            )
        except duckdb.IntegrityError as e:
            raise StorageError(f"Entity conflict: {e}") from e
        except duckdb.Error as e:
            raise StorageError(f"Failed to persist entity: {e}") from e

    def put_assertion(self, assertion: Assertion) -> None:
        """Persist an assertion.

        Args:
            assertion: Assertion to persist

        Raises:
            StorageError: If persistence fails
        """
        # Map unified value field to value_lit/value_ref based on kind
        value_lit = assertion.value if assertion.value_kind == "literal" else None
        value_ref = assertion.value if assertion.value_kind == "ref" else None

        try:
            self.conn.execute(
                """
                INSERT INTO assertion (
                    id, namespace, subject, predicate,
                    value_kind, value_type, value_lit, value_ref,
                    author, acting_as, source, confidence, rationale, model,
                    asserted_at, valid_from, valid_to,
                    status, proposal_id, supersedes, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    assertion.id,
                    assertion.namespace,
                    assertion.subject,
                    assertion.predicate,
                    assertion.value_kind,
                    assertion.value_type,
                    value_lit,
                    value_ref,
                    assertion.author,
                    assertion.acting_as,
                    assertion.source,
                    assertion.confidence,
                    assertion.rationale,
                    assertion.model,
                    assertion.asserted_at.isoformat(),
                    assertion.valid_from.isoformat() if assertion.valid_from else None,
                    assertion.valid_to.isoformat() if assertion.valid_to else None,
                    assertion.status,
                    assertion.proposal_id,
                    assertion.supersedes,
                    json.dumps(assertion.metadata),
                ],
            )
        except duckdb.IntegrityError as e:
            raise StorageError(
                f"Assertion conflict (id={assertion.id}, subject={assertion.subject}): {e}"
            ) from e
        except duckdb.Error as e:
            raise StorageError(f"Failed to persist assertion (id={assertion.id}): {e}") from e

    def get_entity(self, entity_id: str) -> Entity | None:
        """Retrieve an entity by ID.

        Args:
            entity_id: Entity ID to retrieve

        Returns:
            Entity if found, None otherwise
        """
        cursor = self.conn.execute("SELECT * FROM entity WHERE id = ?", [entity_id])
        row = cursor.fetchone()
        if row is None:
            return None

        d = self._row_to_dict(cursor, row)
        return Entity(
            id=d["id"],
            namespace=d["namespace"],
            concept=d["concept"],
            natural_key=d["natural_key"],
            created_at=datetime.fromisoformat(d["created_at"]),
            created_by=d["created_by"],
        )

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
            status: Filter by current status (ignored when as_of_time is set)
            as_of_time: If set, applies bitemporal filter:
                asserted_at <= t AND valid_from <= t AND (valid_to IS NULL OR valid_to > t)
            include_flagged: When as_of_time is set, whether to include
                'flagged' assertions (excluded by default — a flagged
                assertion is disputed, not confirmed-valid; pass True for
                explicit audit/history views)

        Returns:
            List of matching assertions
        """
        query = "SELECT * FROM assertion WHERE 1=1"
        params: list[str] = []

        if subject is not None:
            query += " AND subject = ?"
            params.append(subject)

        if predicate is not None:
            query += " AND predicate = ?"
            params.append(predicate)

        if as_of_time is not None:
            t_iso = as_of_time.isoformat()
            query += " AND asserted_at <= ?"
            params.append(t_iso)
            query += " AND (valid_from IS NULL OR valid_from <= ?)"
            params.append(t_iso)
            query += " AND (valid_to IS NULL OR valid_to > ?)"
            params.append(t_iso)
            if not include_flagged:
                # Flagged-at-t, not current status: a static conflict flags an
                # assertion permanently (no valid_to change), so using current
                # status here would hide it from as_of() queries for times
                # before the dispute existed. Reconstruct from the event log
                # instead — every flagged transition (including an assertion
                # born already-flagged) has a 'flagged' event, see
                # Ontology._apply_with_conflict_routing. "at" is quoted -
                # reserved word in DuckDB. Tiebreak on ae.id: two events can
                # share the same `at` under a clock that hasn't advanced
                # (e.g. flag-then-resolve in the same tick), and `at` alone
                # would make "last recorded wins" nondeterministic. Under
                # SequentialIdProvider/FixedIdProvider (used in tests) id
                # order matches recording order exactly; under the production
                # UlidProvider, id is monotonic across milliseconds but not
                # guaranteed within one, so same-`at` AND same-millisecond
                # ties are a residual (low-probability, not exploitable)
                # nondeterminism.
                query += """ AND COALESCE(
                    (SELECT ae.action FROM assertion_event ae
                     WHERE ae.assertion_id = assertion.id AND ae."at" <= ?
                       AND ae.action IN ('flagged', 'reactivated')
                     ORDER BY ae."at" DESC, ae.id DESC LIMIT 1),
                    'reactivated'
                ) != 'flagged'"""
                params.append(t_iso)
        elif status is not None:
            query += " AND status = ?"
            params.append(status)

        cursor = self.conn.execute(query, params)
        rows = cursor.fetchall()
        return [self._row_to_assertion(self._row_to_dict(cursor, row)) for row in rows]

    @staticmethod
    def _row_to_assertion(row: dict[str, Any]) -> Assertion:
        """Deserialize an `assertion` table row into an Assertion."""
        # Reconstruct unified value from value_lit/value_ref
        value = row["value_lit"] if row["value_kind"] == "literal" else row["value_ref"]

        return Assertion(
            id=row["id"],
            namespace=row["namespace"],
            subject=row["subject"],
            predicate=row["predicate"],
            value_kind=row["value_kind"],
            value_type=row["value_type"],
            value=value,
            author=row["author"],
            acting_as=row["acting_as"],
            source=row["source"],
            confidence=row["confidence"],
            rationale=row["rationale"],
            model=row["model"],
            asserted_at=datetime.fromisoformat(row["asserted_at"]),
            valid_from=(datetime.fromisoformat(row["valid_from"]) if row["valid_from"] else None),
            valid_to=(datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None),
            status=row["status"],
            proposal_id=row["proposal_id"],
            supersedes=row["supersedes"],
            metadata=json.loads(row["metadata"]),
        )

    def get_assertion(self, assertion_id: str) -> Assertion | None:
        """Retrieve a single assertion by ID, regardless of status.

        Args:
            assertion_id: Assertion ID to retrieve

        Returns:
            Assertion if found, None otherwise
        """
        cursor = self.conn.execute("SELECT * FROM assertion WHERE id = ?", [assertion_id])
        row = cursor.fetchone()
        return self._row_to_assertion(self._row_to_dict(cursor, row)) if row else None

    def set_assertion_status(
        self,
        assertion_id: str,
        status: str,
        valid_to: str | None = None,
    ) -> None:
        """Update assertion status and optionally close validity window.

        This is the ONLY allowed mutation on assertions (append-only invariant).

        Args:
            assertion_id: Assertion ID to update
            status: New status (superseded, retracted, flagged)
            valid_to: Optional validity end time (ISO format)

        Raises:
            StorageError: If update fails or assertion not found
        """
        try:
            # Existence check + plain UPDATE, not `UPDATE ... RETURNING`: the
            # assertion table is FK-referenced by assertion_event.assertion_id,
            # and DuckDB's RETURNING raises a false-positive constraint
            # violation when updating a row that's the target of an incoming
            # FK from another table (confirmed empirically, duckdb==1.5.4;
            # plain UPDATE on the same row works correctly).
            if (
                self.conn.execute("SELECT 1 FROM assertion WHERE id = ?", [assertion_id]).fetchone()
                is None
            ):
                raise StorageError(f"Assertion not found: {assertion_id}")

            if valid_to is not None:
                self.conn.execute(
                    "UPDATE assertion SET status = ?, valid_to = ? WHERE id = ?",
                    [status, valid_to, assertion_id],
                )
            else:
                self.conn.execute(
                    "UPDATE assertion SET status = ? WHERE id = ?",
                    [status, assertion_id],
                )
        except duckdb.Error as e:
            raise StorageError(f"Failed to update assertion status (id={assertion_id}): {e}") from e

    def put_schema(self, schema: SchemaIR) -> None:
        """Persist a schema version.

        Also registers ``schema.namespace`` in the namespace registry if
        not already present (KI-022) — a namespace that only ever has a
        schema applied, never an entity, is still discoverable via
        `list_namespaces()`.

        Args:
            schema: Schema to persist

        Raises:
            StorageError: If persistence fails
        """
        try:
            self._ensure_namespace_registered(schema.namespace)
            self.conn.execute(
                """
                INSERT INTO schema_version (namespace, version, definition, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    schema.namespace,
                    schema.version,
                    json.dumps(schema.to_json()),
                    self._clock.now().isoformat(),
                ],
            )
        except duckdb.IntegrityError as e:
            raise StorageError(
                f"Schema conflict (namespace={schema.namespace}, version={schema.version}): {e}"
            ) from e
        except duckdb.Error as e:
            raise StorageError(
                f"Failed to persist schema (namespace={schema.namespace}): {e}"
            ) from e

    def get_schema(self, namespace: str, version: int | None = None) -> SchemaIR | None:
        """Retrieve a schema version.

        Args:
            namespace: Namespace to query
            version: Specific version, or None for latest

        Returns:
            Schema if found, None otherwise
        """
        if version is None:
            # Get latest version
            cursor = self.conn.execute(
                """
                SELECT definition FROM schema_version
                WHERE namespace = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                [namespace],
            )
        else:
            # Get specific version
            cursor = self.conn.execute(
                """
                SELECT definition FROM schema_version
                WHERE namespace = ? AND version = ?
                """,
                [namespace, version],
            )

        row = cursor.fetchone()
        if row is None:
            return None

        definition = json.loads(row[0])
        return SchemaIR.from_json(definition)

    def get_schema_at(self, namespace: str, at: datetime) -> SchemaIR | None:
        """Retrieve the schema version effective at a point in time (KI-019).

        Orders by applied_at (the actual "effective at" moment), with
        version as a tiebreak for same-timestamp rows under a coarse or
        injected Clock — not by version alone, so this stays correct even
        if a future write path ever persisted schema rows out of temporal
        order relative to their version numbers.
        """
        cursor = self.conn.execute(
            """
            SELECT definition FROM schema_version
            WHERE namespace = ? AND applied_at <= ?
            ORDER BY applied_at DESC, version DESC
            LIMIT 1
            """,
            [namespace, at.isoformat()],
        )
        row = cursor.fetchone()
        if row is None:
            return None

        definition = json.loads(row[0])
        return SchemaIR.from_json(definition)

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
        query = "SELECT * FROM entity WHERE 1=1"
        params: list[str] = []

        if namespace is not None:
            query += " AND namespace = ?"
            params.append(namespace)

        if concept is not None:
            query += " AND concept = ?"
            params.append(concept)

        if as_of_time is not None:
            query += " AND created_at <= ?"
            params.append(as_of_time.isoformat())

        cursor = self.conn.execute(query, params)
        rows = cursor.fetchall()

        results = []
        for row in rows:
            d = self._row_to_dict(cursor, row)
            results.append(
                Entity(
                    id=d["id"],
                    namespace=d["namespace"],
                    concept=d["concept"],
                    natural_key=d["natural_key"],
                    created_at=datetime.fromisoformat(d["created_at"]),
                    created_by=d["created_by"],
                )
            )

        return results

    def put_proposal(self, proposal: Proposal) -> None:
        """Persist a proposal."""
        try:
            self.conn.execute(
                """
                INSERT INTO proposal (id, namespace, author, acting_as, state,
                    created_at, decided_at, policy_reason, payload, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    proposal.id,
                    proposal.namespace,
                    proposal.author,
                    proposal.acting_as,
                    proposal.state,
                    proposal.created_at.isoformat(),
                    proposal.decided_at.isoformat() if proposal.decided_at else None,
                    proposal.policy_reason,
                    json.dumps(proposal.payload),
                    json.dumps(proposal.metadata),
                ],
            )
        except duckdb.IntegrityError as e:
            raise StorageError(f"Proposal conflict (id={proposal.id}): {e}") from e
        except duckdb.Error as e:
            raise StorageError(f"Failed to persist proposal (id={proposal.id}): {e}") from e

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        """Retrieve a proposal by ID."""
        cursor = self.conn.execute("SELECT * FROM proposal WHERE id = ?", [proposal_id])
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_proposal(self._row_to_dict(cursor, row))

    @staticmethod
    def _row_to_proposal(row: dict[str, Any]) -> Proposal:
        return Proposal(
            id=row["id"],
            namespace=row["namespace"],
            author=row["author"],
            acting_as=row["acting_as"],
            state=row["state"],
            created_at=datetime.fromisoformat(row["created_at"]),
            decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
            policy_reason=row["policy_reason"],
            payload=json.loads(row["payload"]),
            metadata=json.loads(row["metadata"]),
        )

    def proposals(self, state: str | None = None) -> list[Proposal]:
        """Query proposals, optionally filtered by state (SPEC §14.1)."""
        if state is not None:
            cursor = self.conn.execute(
                "SELECT * FROM proposal WHERE state = ? ORDER BY created_at DESC, id DESC", [state]
            )
        else:
            cursor = self.conn.execute("SELECT * FROM proposal ORDER BY created_at DESC, id DESC")
        rows = cursor.fetchall()
        return [self._row_to_proposal(self._row_to_dict(cursor, row)) for row in rows]

    def update_proposal_state(
        self,
        proposal_id: str,
        state: str,
        decided_at: str | None = None,
        policy_reason: str | None = None,
    ) -> None:
        """Update proposal state after policy decision.

        policy_reason=None leaves the stored value unchanged (COALESCE), it
        does not clear it — see the port docstring for why.
        """
        try:
            # Existence check + plain UPDATE, not `UPDATE ... RETURNING`: the
            # proposal table is FK-referenced by proposal_event.proposal_id —
            # see set_assertion_status for why RETURNING is unsafe here.
            if (
                self.conn.execute("SELECT 1 FROM proposal WHERE id = ?", [proposal_id]).fetchone()
                is None
            ):
                raise StorageError(f"Proposal not found: {proposal_id}")

            self.conn.execute(
                "UPDATE proposal SET state = ?, decided_at = ?, "
                "policy_reason = COALESCE(?, policy_reason) WHERE id = ?",
                [state, decided_at, policy_reason, proposal_id],
            )
        except duckdb.Error as e:
            raise StorageError(f"Failed to update proposal (id={proposal_id}): {e}") from e

    def put_proposal_event(self, event: ProposalEvent) -> None:
        """Persist a structured review-action event (SPEC §9.4)."""
        try:
            self.conn.execute(
                """
                INSERT INTO proposal_event (id, proposal_id, actor, type, detail, "at")
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    event.id,
                    event.proposal_id,
                    event.actor,
                    event.type,
                    event.detail,
                    event.at.isoformat(),
                ],
            )
        except duckdb.IntegrityError as e:
            raise StorageError(f"Proposal event conflict (id={event.id}): {e}") from e
        except duckdb.Error as e:
            raise StorageError(f"Failed to persist proposal event (id={event.id}): {e}") from e

    def get_proposal_events(self, proposal_id: str) -> list[ProposalEvent]:
        """Retrieve all review events for a proposal, oldest first."""
        cursor = self.conn.execute(
            'SELECT * FROM proposal_event WHERE proposal_id = ? ORDER BY "at" ASC',
            [proposal_id],
        )
        rows = cursor.fetchall()
        return [
            ProposalEvent(
                id=d["id"],
                proposal_id=d["proposal_id"],
                actor=d["actor"],
                type=d["type"],
                detail=d["detail"],
                at=datetime.fromisoformat(d["at"]),
            )
            for d in (self._row_to_dict(cursor, row) for row in rows)
        ]

    def put_assertion_event(self, event: AssertionEvent) -> None:
        """Persist an append-only assertion status-mutation event."""
        try:
            self.conn.execute(
                """
                INSERT INTO assertion_event (id, assertion_id, actor, action, "at", successor_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    event.id,
                    event.assertion_id,
                    event.actor,
                    event.action,
                    event.at.isoformat(),
                    event.successor_id,
                ],
            )
        except duckdb.IntegrityError as e:
            raise StorageError(f"Assertion event conflict (id={event.id}): {e}") from e
        except duckdb.Error as e:
            raise StorageError(f"Failed to persist assertion event (id={event.id}): {e}") from e

    def get_assertion_events(self, assertion_id: str) -> list[AssertionEvent]:
        """Retrieve all status-mutation events for an assertion, oldest first."""
        cursor = self.conn.execute(
            'SELECT * FROM assertion_event WHERE assertion_id = ? ORDER BY "at" ASC',
            [assertion_id],
        )
        rows = cursor.fetchall()
        return [
            self._row_to_assertion_event(d)
            for d in (self._row_to_dict(cursor, row) for row in rows)
        ]

    def get_assertion_events_by_successor(self, successor_id: str) -> list[AssertionEvent]:
        """Retrieve all 'superseded' events caused by a given successor assertion."""
        cursor = self.conn.execute(
            'SELECT * FROM assertion_event WHERE successor_id = ? ORDER BY "at" ASC, id ASC',
            [successor_id],
        )
        rows = cursor.fetchall()
        return [
            self._row_to_assertion_event(d)
            for d in (self._row_to_dict(cursor, row) for row in rows)
        ]

    @staticmethod
    def _row_to_assertion_event(d: dict[str, Any]) -> AssertionEvent:
        return AssertionEvent(
            id=d["id"],
            assertion_id=d["assertion_id"],
            actor=d["actor"],
            action=d["action"],
            at=datetime.fromisoformat(d["at"]),
            successor_id=d["successor_id"],
        )

    def put_contradiction(self, contradiction: Contradiction) -> None:
        """Persist a new contradiction."""
        try:
            self.conn.execute(
                """
                INSERT INTO contradiction (id, namespace, subject, predicate, state,
                    member_ids, created_at, raised_by, resolved_by, resolved_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    contradiction.id,
                    contradiction.namespace,
                    contradiction.subject,
                    contradiction.predicate,
                    contradiction.state,
                    json.dumps(contradiction.member_ids),
                    contradiction.created_at.isoformat(),
                    contradiction.raised_by,
                    contradiction.resolved_by,
                    contradiction.resolved_at.isoformat() if contradiction.resolved_at else None,
                    json.dumps(contradiction.metadata),
                ],
            )
        except duckdb.IntegrityError as e:
            raise StorageError(f"Contradiction conflict (id={contradiction.id}): {e}") from e
        except duckdb.Error as e:
            raise StorageError(
                f"Failed to persist contradiction (id={contradiction.id}): {e}"
            ) from e

    @staticmethod
    def _row_to_contradiction(row: dict[str, Any]) -> Contradiction:
        """Deserialize a `contradiction` table row into a Contradiction."""
        return Contradiction(
            id=row["id"],
            namespace=row["namespace"],
            subject=row["subject"],
            predicate=row["predicate"],
            state=row["state"],
            member_ids=json.loads(row["member_ids"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            raised_by=row["raised_by"],
            resolved_by=row["resolved_by"],
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            metadata=json.loads(row["metadata"]),
        )

    def get_open_contradiction(
        self, namespace: str, subject: str, predicate: str
    ) -> Contradiction | None:
        """Return the open contradiction for (namespace, subject, predicate), if any."""
        cursor = self.conn.execute(
            """
            SELECT * FROM contradiction
            WHERE namespace = ? AND subject = ? AND predicate = ? AND state = 'open'
            LIMIT 1
            """,
            [namespace, subject, predicate],
        )
        row = cursor.fetchone()
        return self._row_to_contradiction(self._row_to_dict(cursor, row)) if row else None

    def update_contradiction_members(
        self,
        contradiction_id: str,
        member_ids: list[str],
    ) -> None:
        """Add member IDs to an existing open contradiction."""
        try:
            cursor = self.conn.execute(
                "UPDATE contradiction SET member_ids = ? WHERE id = ? RETURNING id",
                [json.dumps(member_ids), contradiction_id],
            )
            if not cursor.fetchall():
                raise StorageError(f"Contradiction not found: {contradiction_id}")
        except duckdb.Error as e:
            raise StorageError(
                f"Failed to update contradiction (id={contradiction_id}): {e}"
            ) from e

    def get_contradiction(self, contradiction_id: str) -> Contradiction | None:
        """Retrieve a contradiction by ID, regardless of state."""
        cursor = self.conn.execute("SELECT * FROM contradiction WHERE id = ?", [contradiction_id])
        row = cursor.fetchone()
        return self._row_to_contradiction(self._row_to_dict(cursor, row)) if row else None

    def contradictions(self, state: str | None = None) -> list[Contradiction]:
        """Query contradictions, optionally filtered by state (SPEC §14.1)."""
        if state is not None:
            cursor = self.conn.execute(
                "SELECT * FROM contradiction WHERE state = ? ORDER BY created_at DESC, id DESC",
                [state],
            )
        else:
            cursor = self.conn.execute(
                "SELECT * FROM contradiction ORDER BY created_at DESC, id DESC"
            )
        rows = cursor.fetchall()
        return [self._row_to_contradiction(self._row_to_dict(cursor, row)) for row in rows]

    def resolve_contradiction(
        self,
        contradiction_id: str,
        resolved_by: str,
        resolved_at: datetime,
    ) -> None:
        """Mark a contradiction as resolved (SPEC §10.3)."""
        try:
            cursor = self.conn.execute(
                """
                UPDATE contradiction
                SET state = 'resolved', resolved_by = ?, resolved_at = ?
                WHERE id = ?
                RETURNING id
                """,
                [resolved_by, resolved_at.isoformat(), contradiction_id],
            )
            if not cursor.fetchall():
                raise StorageError(f"Contradiction not found: {contradiction_id}")
        except duckdb.Error as e:
            raise StorageError(
                f"Failed to resolve contradiction (id={contradiction_id}): {e}"
            ) from e

    def _validate_scope(self, scope: str) -> None:
        """Raise ValidationError unless scope is one of VECTOR_SCOPES."""
        if scope not in VECTOR_SCOPES:
            raise ValidationError(
                f"Unknown vector scope: {scope!r} (must be one of {sorted(VECTOR_SCOPES)})"
            )

    def _ensure_vector_table(self, scope: str, vec_len: int) -> None:
        """Establish (or validate against) the dimension for `scope`.

        The first vector ever upserted into a scope fixes its dimension: the
        table is created at that width, and later mismatched upserts raise
        ValidationError rather than silently corrupting search results.

        Raises:
            ValidationError: `vec_len` doesn't match the established dim.
            StorageError: If lazy table creation fails.
        """
        row = self.conn.execute("SELECT dim FROM vector_scope WHERE scope = ?", [scope]).fetchone()
        if row is None:
            try:
                self.conn.execute(
                    f"CREATE TABLE vector_{scope} (id TEXT PRIMARY KEY, embedding FLOAT[{vec_len}])"
                )
                self.conn.execute(
                    "INSERT INTO vector_scope (scope, dim) VALUES (?, ?)", [scope, vec_len]
                )
            except duckdb.Error as e:
                raise StorageError(f"Failed to create vector table for scope {scope!r}: {e}") from e
            return
        if row[0] != vec_len:
            raise ValidationError(
                f"Vector for scope {scope!r} has dimension {vec_len}, "
                f"but this scope is established at dimension {row[0]}"
            )

    def vector_upsert(self, scope: str, id: str, vec: list[float]) -> None:
        """Insert or replace the embedding vector for (scope, id).

        Args:
            scope: Embedding scope. Must be one of VECTOR_SCOPES.
            id: Entity or assertion ID the vector represents.
            vec: Embedding vector.

        Raises:
            ValidationError: scope is not in VECTOR_SCOPES, or vec's length
                does not match the scope's already-established dimension.
            StorageError: If persistence fails.
        """
        self._validate_scope(scope)
        self._ensure_vector_table(scope, len(vec))
        try:
            # vector_{scope} is built from `scope`, already validated above
            # against the closed VECTOR_SCOPES set — not caller-controlled
            # free text.
            self.conn.execute(f"INSERT OR REPLACE INTO vector_{scope} VALUES (?, ?)", [id, vec])
        except duckdb.Error as e:
            raise StorageError(f"Failed to upsert vector (scope={scope}, id={id}): {e}") from e

    def vector_search(self, scope: str, vec: list[float], k: int) -> list[tuple[str, float]]:
        """Return the k nearest ids to vec within scope, ascending distance.

        Args:
            scope: Embedding scope. Must be one of VECTOR_SCOPES.
            vec: Query vector.
            k: Maximum number of results.

        Returns:
            (id, distance) tuples, nearest first. Empty list if the scope
            has never been populated.

        Raises:
            ValidationError: scope is not in VECTOR_SCOPES, or vec's length
                does not match the scope's already-established dimension.
        """
        self._validate_scope(scope)
        row = self.conn.execute("SELECT dim FROM vector_scope WHERE scope = ?", [scope]).fetchone()
        if row is None:
            return []
        if row[0] != len(vec):
            raise ValidationError(
                f"Query vector for scope {scope!r} has dimension {len(vec)}, "
                f"but this scope is established at dimension {row[0]}"
            )

        # vector_{scope} is built from `scope`, already validated above
        # (same reasoning as vector_upsert) — not caller-controlled free text.
        rows = self.conn.execute(
            f"SELECT id, list_distance(embedding, ?) AS distance FROM vector_{scope} "  # nosec B608
            "ORDER BY distance LIMIT ?",
            [vec, k],
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def entities_where(
        self,
        namespace: str,
        concept: str,
        predicate_filters: list[tuple[str, str, Any]],
        as_of_time: datetime | None = None,
        include_flagged: bool = False,
    ) -> list[Entity]:
        """Query entities matching all predicate filters in one SQL query.

        Uses correlated subqueries so each filter hits the
        idx_assertion_pred_value/idx_assertion_pred_ref indexes instead of
        doing one round-trip per entity. `"eq"` matches either a literal
        property (`value_lit`) or a relation's target entity id
        (`value_ref`) — KI-030: relation filters like `employer="org-123"`
        are equality checks against `value_ref`, not traversal into the
        target entity's own properties. The two are checked via a UNION ALL
        of two single-column point lookups rather than one `value_lit = ?
        OR value_ref = ?` predicate, matching the SQLite backend (whose
        planner doesn't reliably pick a seekable plan for the OR form).
        `"contains"`/`"gt"`/`"lt"`/`"gte"`/`"lte"` (KI-039) check
        `value_lit` only — see this port method's own docstring for why
        relations don't get a UNION ALL branch for those. Numeric range
        comparisons cast to `DOUBLE`, not `REAL` — this module's own header
        comment notes DuckDB's `REAL` is 4-byte single precision, unlike
        SQLite's always-8-byte `REAL`, which would silently round values.

        Args:
            namespace: Namespace to query
            concept: Concept to filter by
            predicate_filters: List of `(full_predicate, operator, value)`
                triples (AND semantics) — see the port method's docstring
                for the operator set
            as_of_time: If set, applies bitemporal filter on assertions and entity creation
            include_flagged: When as_of_time is set, whether to include
                'flagged' assertions in the predicate match (excluded by
                default — see assertions())

        Returns:
            List of entities where all filters match at the given time
        """
        query = "SELECT * FROM entity WHERE namespace = ? AND concept = ?"
        params: list[Any] = [namespace, concept]

        if as_of_time is not None:
            t_iso = as_of_time.isoformat()
            query += " AND created_at <= ?"
            params.append(t_iso)
            # flagged_clause is always one of exactly two hardcoded literals
            # (never caller-controlled) - not a SQL injection vector despite
            # bandit's B608 heuristic flagging any keyword-string + variable
            # concatenation regardless of the variable's actual provenance.
            flagged_clause = "" if include_flagged else " AND status != 'flagged'"
            match_clause = (
                " AND asserted_at <= ?"
                " AND (valid_from IS NULL OR valid_from <= ?)"
                " AND (valid_to IS NULL OR valid_to > ?)"
                f"{flagged_clause}"  # nosec B608
            )
            match_params = [t_iso, t_iso, t_iso]
        else:
            match_clause = " AND status = 'active'"
            match_params = []

        # predicate/value are always bound via `?` below, never
        # interpolated; the only interpolated piece is match_clause, itself
        # built from hardcoded literals (see the flagged_clause
        # justification above) - same already-justified pattern, not a new
        # SQL injection surface.
        for predicate, operator, value in predicate_filters:
            if operator == "eq":
                query += (
                    " AND id IN ("  # nosec B608
                    "SELECT subject FROM assertion"
                    f" WHERE predicate = ? AND value_lit = ?{match_clause}"
                    " UNION ALL "
                    "SELECT subject FROM assertion"
                    f" WHERE predicate = ? AND value_ref = ?{match_clause}"
                    ")"
                )
                params.extend([predicate, value, *match_params, predicate, value, *match_params])
            elif operator == "contains":
                query += (
                    " AND id IN ("  # nosec B608
                    "SELECT subject FROM assertion"
                    f" WHERE predicate = ? AND value_lit LIKE ? ESCAPE '\\'{match_clause}"
                    ")"
                )
                params.extend([predicate, f"%{_like_escape(value)}%", *match_params])
            else:
                sql_op = _RANGE_SQL_OPERATORS[operator]
                query += (
                    " AND id IN ("  # nosec B608
                    "SELECT subject FROM assertion"
                    f" WHERE predicate = ? AND CAST(value_lit AS DOUBLE) {sql_op} ?{match_clause}"
                    ")"
                )
                params.extend([predicate, value, *match_params])

        cursor = self.conn.execute(query, params)
        rows = cursor.fetchall()

        return [
            Entity(
                id=d["id"],
                namespace=d["namespace"],
                concept=d["concept"],
                natural_key=d["natural_key"],
                created_at=datetime.fromisoformat(d["created_at"]),
                created_by=d["created_by"],
            )
            for d in (self._row_to_dict(cursor, row) for row in rows)
        ]

    def entities_meeting_confidence(
        self,
        namespace: str,
        concept: str,
        threshold: float,
        as_of_time: datetime | None = None,
        candidate_ids: frozenset[str] | None = None,
    ) -> set[str]:
        """IDs of entities in `(namespace, concept)` with >=1 assertion at or
        above `threshold` confidence, active at `as_of_time` (KI-036) or
        currently active if `as_of_time` is None. `candidate_ids`, if given,
        narrows the scan below `(namespace, concept)` via `unnest()` (KI-037)
        — `QueryBuilder` only ever passes a set bounded by
        `_CANDIDATE_HINT_MAX` (1000), the range measured to be a genuine win
        here (~2-3x at 10k-50k entities); an earlier, unbounded version of
        this hint measured `unnest()` as a regression on both very small
        candidate sets (~1k entities, where everything is already fast) and
        large ones (thousands of candidates against a 10k-entity concept,
        >100x slower) — bounding the hint's size, not avoiding `unnest()`
        altogether, is what makes it a reliable win."""
        if candidate_ids is not None and not candidate_ids:
            return set()

        query = (
            "SELECT DISTINCT a.subject FROM assertion a"
            " JOIN entity e ON e.id = a.subject"
            " WHERE e.namespace = ? AND e.concept = ? AND a.confidence >= ?"
        )
        params: list[Any] = [namespace, concept, threshold]

        if as_of_time is not None:
            t_iso = as_of_time.isoformat()
            query += (
                " AND a.status != 'flagged'"
                " AND a.asserted_at <= ?"
                " AND (a.valid_from IS NULL OR a.valid_from <= ?)"
                " AND (a.valid_to IS NULL OR a.valid_to > ?)"
            )
            params.extend([t_iso, t_iso, t_iso])
        else:
            query += " AND a.status = 'active'"

        if candidate_ids is not None:
            query += " AND e.id IN (SELECT unnest(?))"
            params.append(list(candidate_ids))

        cursor = self.conn.execute(query, params)
        return {row[0] for row in cursor.fetchall()}

    def entities_meeting_trust(
        self,
        namespace: str,
        concept: str,
        min_trust: int,
        as_of_time: datetime | None = None,
        candidate_ids: frozenset[str] | None = None,
    ) -> set[str]:
        """IDs of entities in `(namespace, concept)` with >=1 assertion,
        active at `as_of_time` (KI-036) or currently active if `as_of_time`
        is None, authored by a principal whose current trust_level >=
        `min_trust`. `candidate_ids` narrows the scan the same way as
        `entities_meeting_confidence` (KI-037) — see its docstring."""
        if candidate_ids is not None and not candidate_ids:
            return set()

        query = (
            "SELECT DISTINCT a.subject FROM assertion a"
            " JOIN entity e ON e.id = a.subject"
            " JOIN principal p ON p.id = a.author"
            " WHERE e.namespace = ? AND e.concept = ? AND p.trust_level >= ?"
        )
        params: list[Any] = [namespace, concept, min_trust]

        if as_of_time is not None:
            t_iso = as_of_time.isoformat()
            query += (
                " AND a.status != 'flagged'"
                " AND a.asserted_at <= ?"
                " AND (a.valid_from IS NULL OR a.valid_from <= ?)"
                " AND (a.valid_to IS NULL OR a.valid_to > ?)"
            )
            params.extend([t_iso, t_iso, t_iso])
        else:
            query += " AND a.status = 'active'"

        if candidate_ids is not None:
            query += " AND e.id IN (SELECT unnest(?))"
            params.append(list(candidate_ids))

        cursor = self.conn.execute(query, params)
        return {row[0] for row in cursor.fetchall()}

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()


__all__ = ["DuckDBBackend"]
