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

from ontolith.core import Assertion, AssertionEvent, Clock, Entity, SystemClock
from ontolith.core.errors import StorageError
from ontolith.govern.contradiction import Contradiction
from ontolith.govern.proposal import Proposal, ProposalEvent
from ontolith.identity import Principal, PrincipalCredential
from ontolith.schema import SchemaIR


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
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS assertion_event (
                id TEXT PRIMARY KEY,
                assertion_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('superseded', 'flagged', 'retracted', 'reactivated')),
                "at" TEXT NOT NULL,
                FOREIGN KEY(actor) REFERENCES principal(id)
            )
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_event_assertion
            ON assertion_event(assertion_id)
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
        # Scoped to accept/reject, the two review actions that exist as
        # Ontology methods; assign/comment/request_changes are not
        # implemented yet (see ProposalEvent docstring).
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
                type TEXT NOT NULL CHECK(type IN ('accept', 'reject')),
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

    @staticmethod
    def _row_to_dict(cursor: duckdb.DuckDBPyConnection, row: tuple[Any, ...]) -> dict[str, Any]:
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

    @staticmethod
    def _row_to_principal(row: dict[str, Any]) -> Principal:
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
        cursor = self.conn.execute(
            "SELECT * FROM principal_credential WHERE principal_id = ? ORDER BY created_at DESC",
            [principal_id],
        )
        return [
            self._row_to_credential(self._row_to_dict(cursor, row)) for row in cursor.fetchall()
        ]

    @staticmethod
    def _row_to_credential(row: dict[str, Any]) -> PrincipalCredential:
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
                query += " AND status != 'flagged'"
        elif status is not None:
            query += " AND status = ?"
            params.append(status)

        cursor = self.conn.execute(query, params)
        rows = cursor.fetchall()
        return [self._row_to_assertion(self._row_to_dict(cursor, row)) for row in rows]

    @staticmethod
    def _row_to_assertion(row: dict[str, Any]) -> Assertion:
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

        Args:
            schema: Schema to persist

        Raises:
            StorageError: If persistence fails
        """
        try:
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

        d = self._row_to_dict(cursor, row)
        return Proposal(
            id=d["id"],
            namespace=d["namespace"],
            author=d["author"],
            acting_as=d["acting_as"],
            state=d["state"],
            created_at=datetime.fromisoformat(d["created_at"]),
            decided_at=datetime.fromisoformat(d["decided_at"]) if d["decided_at"] else None,
            policy_reason=d["policy_reason"],
            payload=json.loads(d["payload"]),
            metadata=json.loads(d["metadata"]),
        )

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
                INSERT INTO assertion_event (id, assertion_id, actor, action, "at")
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    event.id,
                    event.assertion_id,
                    event.actor,
                    event.action,
                    event.at.isoformat(),
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
            AssertionEvent(
                id=d["id"],
                assertion_id=d["assertion_id"],
                actor=d["actor"],
                action=d["action"],
                at=datetime.fromisoformat(d["at"]),
            )
            for d in (self._row_to_dict(cursor, row) for row in rows)
        ]

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

    def entities_where(
        self,
        namespace: str,
        concept: str,
        predicate_filters: dict[str, str],
        as_of_time: datetime | None = None,
        include_flagged: bool = False,
    ) -> list[Entity]:
        """Query entities matching all predicate=value filters in one SQL query.

        Uses correlated subqueries so each (predicate, value_lit) pair hits the
        idx_assertion_spo index instead of doing one round-trip per entity.

        Args:
            namespace: Namespace to query
            concept: Concept to filter by
            predicate_filters: Dict of full_predicate → literal_value (AND semantics)
            as_of_time: If set, applies bitemporal filter on assertions and entity creation
            include_flagged: When as_of_time is set, whether to include
                'flagged' assertions in the predicate match (excluded by
                default — see assertions())

        Returns:
            List of entities where all filters match at the given time
        """
        query = "SELECT * FROM entity WHERE namespace = ? AND concept = ?"
        params: list[str] = [namespace, concept]

        if as_of_time is not None:
            t_iso = as_of_time.isoformat()
            query += " AND created_at <= ?"
            params.append(t_iso)
            flagged_clause = "" if include_flagged else " AND status != 'flagged'"
            for predicate, value in predicate_filters.items():
                query += (
                    " AND id IN ("
                    "SELECT subject FROM assertion"
                    " WHERE predicate = ? AND value_lit = ?"
                    " AND asserted_at <= ?"
                    " AND (valid_from IS NULL OR valid_from <= ?)"
                    " AND (valid_to IS NULL OR valid_to > ?)"
                    f"{flagged_clause}"
                    ")"
                )
                params.extend([predicate, value, t_iso, t_iso, t_iso])
        else:
            for predicate, value in predicate_filters.items():
                query += (
                    " AND id IN ("
                    "SELECT subject FROM assertion"
                    " WHERE predicate = ? AND value_lit = ? AND status = 'active'"
                    ")"
                )
                params.extend([predicate, value])

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

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()


__all__ = ["DuckDBBackend"]
