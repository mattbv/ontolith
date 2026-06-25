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
        self._create_schema()

    def _create_schema(self) -> None:
        """Create database schema if not exists."""
        cursor = self.conn.cursor()

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
                value TEXT NOT NULL,
                author TEXT NOT NULL,
                acting_as TEXT,
                source TEXT,
                confidence REAL CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
                rationale TEXT,
                model TEXT,
                asserted_at TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'retracted', 'flagged')),
                proposal_id TEXT,
                supersedes TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(subject) REFERENCES entity(id)
            )
        """)

        # Index for common queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_subject
            ON assertion(subject, status)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_predicate
            ON assertion(predicate, status)
        """)

        # Index for bitemporal queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_temporal
            ON assertion(valid_from, valid_to, asserted_at)
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

        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO assertion (
                    id, namespace, subject, predicate,
                    value_kind, value_type, value,
                    author, acting_as, source, confidence, rationale, model,
                    asserted_at, valid_from, valid_to,
                    status, proposal_id, supersedes, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assertion.id,
                    assertion.namespace,
                    assertion.subject,
                    assertion.predicate,
                    assertion.value_kind,
                    assertion.value_type,
                    assertion.value,
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
            raise StorageError(f"Assertion conflict: {e}") from e
        except sqlite3.Error as e:
            raise StorageError(f"Failed to persist assertion: {e}") from e

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
            results.append(
                Assertion(
                    id=row["id"],
                    namespace=row["namespace"],
                    subject=row["subject"],
                    predicate=row["predicate"],
                    value_kind=row["value_kind"],
                    value_type=row["value_type"],
                    value=row["value"],
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

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()


__all__ = ["SQLiteBackend"]
