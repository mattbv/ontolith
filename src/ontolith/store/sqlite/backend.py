"""SQLite storage backend implementation.

Default storage adapter for Ontolith. Provides:
- Append-only entity and assertion storage
- Transaction management
- Query interface
- Vector search via sqlite-vec (future)
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from ontolith.core import Assertion, Entity
from ontolith.core.errors import StorageError
from ontolith.identity import Principal


class SQLiteBackend:
    """SQLite implementation of StorageBackend.

    Schema follows SPEC §12.2:
    - entity table with ULID primary key
    - assertion table with ULID primary key
    - Bitemporal columns (asserted_at, valid_from, valid_to)
    - Status tracking for append-only invariant
    """

    def __init__(self, path: str | Path) -> None:
        """Initialize SQLite backend.

        Args:
            path: Path to SQLite database file (created if doesn't exist)
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row  # Enable column access by name
        self.conn.execute("PRAGMA foreign_keys = ON")  # Enable FK constraints
        self._create_schema()

    def _create_schema(self) -> None:
        """Create database schema if not exists."""
        cursor = self.conn.cursor()

        # Principal table (SPEC §8)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS principal (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('human', 'ai', 'service')),
                owner TEXT,
                auth_method TEXT NOT NULL CHECK(auth_method IN ('oidc', 'workload', 'apikey')),
                default_capability TEXT NOT NULL DEFAULT 'propose' CHECK(default_capability IN ('read', 'propose', 'write', 'review', 'admin')),
                trust_level INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """)

        # Entity table (SPEC §12.2)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS entity (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                concept TEXT NOT NULL,
                natural_key TEXT,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                UNIQUE(namespace, concept, natural_key)
            )
        """)

        # Assertion table (SPEC §12.2)
        cursor.execute("""
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
                confidence REAL CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
                rationale TEXT,
                model TEXT,
                asserted_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'retracted', 'flagged')),
                proposal_id TEXT,
                supersedes TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(subject) REFERENCES entity(id)
            )
        """)

        # Indexes (SPEC §12.2)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_spo
            ON assertion(namespace, subject, predicate, status)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_subj
            ON assertion(namespace, subject)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_time
            ON assertion(asserted_at)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_valid
            ON assertion(valid_from, valid_to)
        """)

        self.conn.commit()

    def begin(self) -> None:
        """Begin a new transaction."""
        # SQLite is in autocommit mode by default, explicit BEGIN
        self.conn.execute("BEGIN")

    def commit(self) -> None:
        """Commit the current transaction."""
        try:
            self.conn.commit()
        except sqlite3.Error as e:
            raise StorageError(f"Failed to commit transaction: {e}") from e

    def rollback(self) -> None:
        """Rollback the current transaction."""
        try:
            self.conn.rollback()
        except sqlite3.Error as e:
            raise StorageError(f"Failed to rollback transaction: {e}") from e

    def put_principal(self, principal: Principal) -> None:
        """Persist a principal.

        Args:
            principal: Principal to persist

        Raises:
            StorageError: If persistence fails
        """
        import json

        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO principal (id, kind, owner, auth_method, default_capability, trust_level, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    principal.id,
                    principal.kind,
                    principal.owner,
                    principal.auth_method,
                    principal.default_capability,
                    principal.trust_level,
                    principal.created_at.isoformat(),
                    json.dumps(principal.metadata),
                ),
            )
        except sqlite3.IntegrityError as e:
            raise StorageError(f"Principal conflict (id={principal.id}): {e}") from e
        except sqlite3.Error as e:
            raise StorageError(f"Failed to persist principal (id={principal.id}): {e}") from e

    def get_principal(self, principal_id: str) -> Principal | None:
        """Retrieve a principal by ID.

        Args:
            principal_id: Principal ID to retrieve

        Returns:
            Principal if found, None otherwise
        """
        import json

        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM principal WHERE id = ?",
            (principal_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

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

    def put_entity(self, entity: Entity) -> None:
        """Persist an entity.

        Args:
            entity: Entity to persist

        Raises:
            StorageError: If persistence fails
        """
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO entity (id, namespace, concept, natural_key, created_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entity.id,
                    entity.namespace,
                    entity.concept,
                    entity.natural_key,
                    entity.created_at.isoformat(),
                    entity.created_by,
                ),
            )
        except sqlite3.IntegrityError as e:
            raise StorageError(f"Entity conflict: {e}") from e
        except sqlite3.Error as e:
            raise StorageError(f"Failed to persist entity: {e}") from e

    def put_assertion(self, assertion: Assertion) -> None:
        """Persist an assertion.

        Args:
            assertion: Assertion to persist

        Raises:
            StorageError: If persistence fails
        """
        import json

        # Map unified value field to value_lit/value_ref based on kind
        value_lit = assertion.value if assertion.value_kind == "literal" else None
        value_ref = assertion.value if assertion.value_kind == "ref" else None

        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO assertion (
                    id, namespace, subject, predicate,
                    value_kind, value_type, value_lit, value_ref,
                    author, acting_as, source, confidence, rationale, model,
                    asserted_at, valid_from, valid_to,
                    status, proposal_id, supersedes, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
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
                ),
            )
        except sqlite3.IntegrityError as e:
            raise StorageError(
                f"Assertion conflict (id={assertion.id}, subject={assertion.subject}): {e}"
            ) from e
        except sqlite3.Error as e:
            raise StorageError(
                f"Failed to persist assertion (id={assertion.id}): {e}"
            ) from e

    def get_entity(self, entity_id: str) -> Entity | None:
        """Retrieve an entity by ID.

        Args:
            entity_id: Entity ID to retrieve

        Returns:
            Entity if found, None otherwise
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM entity WHERE id = ?",
            (entity_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return Entity(
            id=row["id"],
            namespace=row["namespace"],
            concept=row["concept"],
            natural_key=row["natural_key"],
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
        )

    def assertions(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        status: str | None = "active",
    ) -> list[Assertion]:
        """Query assertions with optional filters.

        Args:
            subject: Filter by subject entity ID
            predicate: Filter by predicate
            status: Filter by status (default: active only, None = all)

        Returns:
            List of matching assertions
        """
        import json

        query = "SELECT * FROM assertion WHERE 1=1"
        params: list[str] = []

        if subject is not None:
            query += " AND subject = ?"
            params.append(subject)

        if predicate is not None:
            query += " AND predicate = ?"
            params.append(predicate)

        if status is not None:
            query += " AND status = ?"
            params.append(status)

        cursor = self.conn.cursor()
        cursor.execute(query, params)

        results = []
        for row in cursor.fetchall():
            # Reconstruct unified value from value_lit/value_ref
            value = row["value_lit"] if row["value_kind"] == "literal" else row["value_ref"]

            results.append(
                Assertion(
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
                    valid_from=(
                        datetime.fromisoformat(row["valid_from"])
                        if row["valid_from"]
                        else None
                    ),
                    valid_to=(
                        datetime.fromisoformat(row["valid_to"])
                        if row["valid_to"]
                        else None
                    ),
                    status=row["status"],
                    proposal_id=row["proposal_id"],
                    supersedes=row["supersedes"],
                    metadata=json.loads(row["metadata"]),
                )
            )

        return results

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
            cursor = self.conn.cursor()
            if valid_to is not None:
                cursor.execute(
                    "UPDATE assertion SET status = ?, valid_to = ? WHERE id = ?",
                    (status, valid_to, assertion_id),
                )
            else:
                cursor.execute(
                    "UPDATE assertion SET status = ? WHERE id = ?",
                    (status, assertion_id),
                )

            if cursor.rowcount == 0:
                raise StorageError(f"Assertion not found: {assertion_id}")

        except sqlite3.Error as e:
            raise StorageError(
                f"Failed to update assertion status (id={assertion_id}): {e}"
            ) from e

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()


__all__ = ["SQLiteBackend"]
