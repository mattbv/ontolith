"""Unit tests for SQLite storage backend."""

import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith.core import Assertion, Entity, FixedClock
from ontolith.core.errors import StorageError
from ontolith.govern.contradiction import Contradiction
from ontolith.govern.proposal import Proposal, ProposalEvent
from ontolith.identity import AdminEvent, Principal, PrincipalCredential
from ontolith.schema import ConceptDef, PropertyDef, SchemaIR
from ontolith.store.sqlite import SQLiteBackend


@pytest.fixture
def temp_db() -> Path:
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        return Path(f.name)


@pytest.fixture
def backend(temp_db: Path) -> SQLiteBackend:
    """Create a SQLite backend with a pre-seeded principal for FK compliance."""
    backend = SQLiteBackend(temp_db)
    backend.put_principal(
        Principal(
            id="alice@test.com",
            kind="human",
            auth_method="oidc",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
    )
    yield backend
    backend.close()
    temp_db.unlink()


class TestSQLiteBackend:
    """Tests for SQLiteBackend."""

    def test_create_backend_creates_schema(self, temp_db: Path) -> None:
        """Backend initialization creates schema tables."""
        backend = SQLiteBackend(temp_db)
        cursor = backend.conn.cursor()

        # Check tables exist
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('entity', 'assertion')"
        )
        tables = {row[0] for row in cursor.fetchall()}
        assert tables == {"entity", "assertion"}

        backend.close()

    def test_wal_mode_enabled(self, temp_db: Path) -> None:
        """SPEC §12.1 MUST: default backend uses WAL journal mode."""
        backend = SQLiteBackend(temp_db)
        mode = backend.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        backend.close()

    def test_put_and_get_entity(self, backend: SQLiteBackend) -> None:
        """Entity can be persisted and retrieved."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            natural_key="ada",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )

        backend.put_entity(entity)
        retrieved = backend.get_entity("entity-001")

        assert retrieved is not None
        assert retrieved.id == entity.id
        assert retrieved.namespace == entity.namespace
        assert retrieved.concept == entity.concept
        assert retrieved.natural_key == entity.natural_key
        assert retrieved.created_by == entity.created_by

    def test_get_nonexistent_entity_returns_none(self, backend: SQLiteBackend) -> None:
        """Getting a nonexistent entity returns None."""
        assert backend.get_entity("nonexistent") is None

    def test_get_entity_by_natural_key(self, backend: SQLiteBackend) -> None:
        """KI-091: an entity can be looked up by its (namespace, concept,
        natural_key) triple, independent of its id."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            natural_key="ada",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)

        retrieved = backend.get_entity_by_natural_key("test-ns", "Person", "ada")

        assert retrieved is not None
        assert retrieved.id == entity.id

    def test_get_entity_by_natural_key_no_match_returns_none(self, backend: SQLiteBackend) -> None:
        """KI-091: a mismatched namespace, concept, or natural_key is not a match."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            natural_key="ada",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)

        assert backend.get_entity_by_natural_key("other-ns", "Person", "ada") is None
        assert backend.get_entity_by_natural_key("test-ns", "Organization", "ada") is None
        assert backend.get_entity_by_natural_key("test-ns", "Person", "grace") is None

    def test_put_assertion(self, backend: SQLiteBackend) -> None:
        """Assertion can be persisted."""
        # First create an entity
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)

        # Create assertion
        assertion = Assertion(
            id="assertion-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada Lovelace",
            author="alice@test.com",
            source="test",
            confidence=0.95,
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        backend.put_assertion(assertion)

        # Verify it was stored
        assertions = backend.assertions(subject="entity-001")
        assert len(assertions) == 1
        assert assertions[0].id == assertion.id
        assert assertions[0].value == "Ada Lovelace"

    def test_get_assertion_by_id(self, backend: SQLiteBackend) -> None:
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)
        assertion = Assertion(
            id="assertion-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada Lovelace",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        backend.put_assertion(assertion)

        fetched = backend.get_assertion("assertion-001")
        assert fetched is not None
        assert fetched.value == "Ada Lovelace"

    def test_get_assertion_returns_none_when_not_found(self, backend: SQLiteBackend) -> None:
        assert backend.get_assertion("nonexistent") is None

    def test_get_assertion_finds_non_active_status(self, backend: SQLiteBackend) -> None:
        """get_assertion must be status-agnostic, unlike the default assertions() filter."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)
        assertion = Assertion(
            id="assertion-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada Lovelace",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
            status="retracted",
        )
        backend.put_assertion(assertion)

        fetched = backend.get_assertion("assertion-001")
        assert fetched is not None
        assert fetched.status == "retracted"

    def test_assertions_filter_by_subject(self, backend: SQLiteBackend) -> None:
        """Assertions can be filtered by subject."""
        # Create entities
        entity1 = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        entity2 = Entity(
            id="entity-002",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity1)
        backend.put_entity(entity2)

        # Create assertions for different entities
        a1 = Assertion(
            id="assertion-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        a2 = Assertion(
            id="assertion-002",
            namespace="test-ns",
            subject="entity-002",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Grace",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        backend.put_assertion(a1)
        backend.put_assertion(a2)

        # Filter by subject
        results = backend.assertions(subject="entity-001")
        assert len(results) == 1
        assert results[0].value == "Ada"

    def test_assertions_filter_by_predicate(self, backend: SQLiteBackend) -> None:
        """Assertions can be filtered by predicate."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)

        # Different predicates
        a1 = Assertion(
            id="assertion-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        a2 = Assertion(
            id="assertion-002",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.born",
            value_kind="literal",
            value_type="Date",
            value="1815-12-10",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        backend.put_assertion(a1)
        backend.put_assertion(a2)

        results = backend.assertions(predicate="Person.name")
        assert len(results) == 1
        assert results[0].value == "Ada"

    def test_assertions_filter_by_status(self, backend: SQLiteBackend) -> None:
        """Assertions can be filtered by status."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)

        # Active assertion
        a1 = Assertion(
            id="assertion-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author="alice@test.com",
            status="active",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        # Retracted assertion
        a2 = Assertion(
            id="assertion-002",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Wrong",
            author="alice@test.com",
            status="retracted",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        backend.put_assertion(a1)
        backend.put_assertion(a2)

        # Default: active only
        active = backend.assertions(subject="entity-001")
        assert len(active) == 1
        assert active[0].status == "active"

        # All statuses
        all_assertions = backend.assertions(subject="entity-001", status=None)
        assert len(all_assertions) == 2

    def test_transaction_commit(self, backend: SQLiteBackend) -> None:
        """Transactions can be committed."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )

        backend.begin()
        backend.put_entity(entity)
        backend.commit()

        # Verify entity persisted
        assert backend.get_entity("entity-001") is not None

    def test_transaction_rollback(self, backend: SQLiteBackend) -> None:
        """Transactions can be rolled back."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )

        backend.begin()
        backend.put_entity(entity)
        backend.rollback()

        # Verify entity NOT persisted
        assert backend.get_entity("entity-001") is None

    def test_metadata_roundtrip(self, backend: SQLiteBackend) -> None:
        """Assertion metadata is preserved through storage."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)

        metadata = {"key": "value", "nested": {"foo": "bar"}}
        assertion = Assertion(
            id="assertion-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
            metadata=metadata,
        )
        backend.put_assertion(assertion)

        retrieved = backend.assertions(subject="entity-001")[0]
        assert retrieved.metadata == metadata

    def test_set_assertion_status(self, backend: SQLiteBackend) -> None:
        """Assertion status can be updated (append-only allowed mutation)."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)

        assertion = Assertion(
            id="assertion-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        backend.put_assertion(assertion)

        # Update status
        backend.set_assertion_status("assertion-001", "retracted")

        # Verify update
        retrieved = backend.assertions(subject="entity-001", status=None)[0]
        assert retrieved.status == "retracted"

    def test_set_assertion_status_with_valid_to(self, backend: SQLiteBackend) -> None:
        """Assertion status update can close validity window."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)

        assertion = Assertion(
            id="assertion-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.employer",
            value_kind="ref",
            value="org-001",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        backend.put_assertion(assertion)

        # Supersede with validity end
        end_time = datetime(2025, 6, 1, tzinfo=UTC).isoformat()
        backend.set_assertion_status("assertion-001", "superseded", valid_to=end_time)

        # Verify both fields updated
        retrieved = backend.assertions(subject="entity-001", status=None)[0]
        assert retrieved.status == "superseded"
        assert retrieved.valid_to == datetime(2025, 6, 1, tzinfo=UTC)

    def test_set_assertion_status_nonexistent_raises(self, backend: SQLiteBackend) -> None:
        """Setting status on nonexistent assertion raises StorageError."""
        from ontolith.core.errors import StorageError

        with pytest.raises(StorageError, match="not found"):
            backend.set_assertion_status("nonexistent", "retracted")

    def test_assertion_events_by_successor_recovers_full_predecessor_set(
        self, backend: SQLiteBackend
    ) -> None:
        """KI-008: querying by successor_id recovers every predecessor a single
        assertion superseded, not just the one recorded in Assertion.supersedes."""
        from ontolith.core import AssertionEvent

        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)
        for aid in ("assertion-001", "assertion-002", "assertion-successor"):
            backend.put_assertion(
                Assertion(
                    id=aid,
                    namespace="test-ns",
                    subject="entity-001",
                    predicate="Person.employer",
                    value_kind="literal",
                    value_type="Text",
                    value=aid,
                    author="alice@test.com",
                    asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
                )
            )

        backend.put_assertion_event(
            AssertionEvent(
                id="event-1",
                assertion_id="assertion-001",
                actor="alice@test.com",
                action="superseded",
                at=datetime(2025, 1, 2, tzinfo=UTC),
                successor_id="assertion-successor",
            )
        )
        backend.put_assertion_event(
            AssertionEvent(
                id="event-2",
                assertion_id="assertion-002",
                actor="alice@test.com",
                action="superseded",
                at=datetime(2025, 1, 2, tzinfo=UTC),
                successor_id="assertion-successor",
            )
        )

        events = backend.get_assertion_events_by_successor("assertion-successor")
        assert {e.assertion_id for e in events} == {"assertion-001", "assertion-002"}
        assert all(e.successor_id == "assertion-successor" for e in events)
        assert backend.get_assertion_events_by_successor("nonexistent") == []

    def test_duplicate_natural_key_raises(self, backend: SQLiteBackend) -> None:
        """Inserting entity with duplicate natural_key raises StorageError."""
        from ontolith.core.errors import StorageError

        e1 = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            natural_key="ada",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(e1)

        e2 = Entity(
            id="entity-002",
            namespace="test-ns",
            concept="Person",
            natural_key="ada",  # Duplicate!
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )

        with pytest.raises(StorageError, match="conflict"):
            backend.put_entity(e2)

    def test_assertion_without_entity_raises(self, backend: SQLiteBackend) -> None:
        """Assertion with invalid subject (FOREIGN KEY) raises StorageError."""
        from ontolith.core.errors import StorageError

        assertion = Assertion(
            id="assertion-001",
            namespace="test-ns",
            subject="nonexistent-entity",  # Invalid!
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        with pytest.raises(StorageError):
            backend.put_assertion(assertion)

    def test_put_and_get_principal(self, backend: SQLiteBackend) -> None:
        """Principal can be persisted and retrieved."""
        principal = Principal(
            id="alice@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            trust_level=10,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        backend.put_principal(principal)
        retrieved = backend.get_principal("alice@example.com")

        assert retrieved is not None
        assert retrieved.id == principal.id
        assert retrieved.kind == principal.kind
        assert retrieved.default_capability == principal.default_capability
        assert retrieved.trust_level == principal.trust_level

    def test_get_nonexistent_principal_returns_none(self, backend: SQLiteBackend) -> None:
        """Getting a nonexistent principal returns None."""
        assert backend.get_principal("nonexistent") is None

    def test_list_principals_returns_all_most_recent_first(self, backend: SQLiteBackend) -> None:
        """list_principals (KI-022) returns every principal, newest created_at first."""
        backend.put_principal(
            Principal(
                id="bob@test.com",
                kind="human",
                auth_method="oidc",
                created_at=datetime(2025, 1, 2, tzinfo=UTC),
            )
        )
        backend.put_principal(
            Principal(
                id="carol@test.com",
                kind="human",
                auth_method="oidc",
                created_at=datetime(2025, 1, 3, tzinfo=UTC),
            )
        )

        principals = backend.list_principals()

        # backend fixture seeds alice@test.com at 2025-01-01
        assert [p.id for p in principals] == ["carol@test.com", "bob@test.com", "alice@test.com"]

    def test_list_principals_same_created_at_tiebreaks_by_id_desc(
        self, backend: SQLiteBackend
    ) -> None:
        """Two principals sharing a created_at (coarse/injected Clock) still
        sort deterministically, via a lexical id DESC tiebreak."""
        same_time = datetime(2025, 1, 5, tzinfo=UTC)
        backend.put_principal(
            Principal(id="aaa@test.com", kind="human", auth_method="oidc", created_at=same_time)
        )
        backend.put_principal(
            Principal(id="zzz@test.com", kind="human", auth_method="oidc", created_at=same_time)
        )

        principals = backend.list_principals()

        tied = [p.id for p in principals if p.id in ("aaa@test.com", "zzz@test.com")]
        assert tied == ["zzz@test.com", "aaa@test.com"]

    def test_default_namespace_seeded_on_fresh_backend(self, backend: SQLiteBackend) -> None:
        """list_namespaces() (KI-022) returns the seeded default namespace
        even with zero entities/schema written - proves this isn't a
        SELECT DISTINCT over some other table (ADR-0022's open question)."""
        namespaces = backend.list_namespaces()
        assert [n.id for n in namespaces] == ["default"]

    def test_default_namespace_not_reseeded_on_reconnect(self, temp_db: Path) -> None:
        """Reconnecting to an existing database file doesn't duplicate or
        re-timestamp the seeded default namespace row."""
        first = SQLiteBackend(temp_db)
        [before] = first.list_namespaces()
        first.close()

        second = SQLiteBackend(temp_db)
        [after] = second.list_namespaces()
        second.close()

        assert after.id == before.id
        assert after.created_at == before.created_at

    def test_list_namespaces_ordering(self, backend: SQLiteBackend) -> None:
        """Most-recently-created namespace first, same convention as
        list_principals - inserted directly since there's no put_namespace
        on the port (namespace creation is out of scope for KI-022)."""
        backend.conn.execute(
            "INSERT INTO namespace (id, created_at, metadata) VALUES (?, ?, ?)",
            ("acme-research", "2030-01-01T00:00:00+00:00", '{"team": "research"}'),
        )
        backend.conn.commit()

        namespaces = backend.list_namespaces()

        assert [n.id for n in namespaces] == ["acme-research", "default"]
        assert namespaces[0].metadata == {"team": "research"}

    def test_principal_metadata_roundtrip(self, backend: SQLiteBackend) -> None:
        """Principal metadata is preserved through storage."""
        metadata = {"team": "engineering", "region": "us-west"}
        principal = Principal(
            id="bot-001",
            kind="ai",
            owner="alice@test.com",  # matches the backend fixture's seeded principal
            auth_method="workload",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            metadata=metadata,
        )

        backend.put_principal(principal)
        retrieved = backend.get_principal("bot-001")

        assert retrieved is not None
        assert retrieved.metadata == metadata

    def test_trust_level_out_of_range_raises(self, backend: SQLiteBackend) -> None:
        """Trust level outside 0-10 range rejected by database (ADR-0009)."""

        # Bypasses Pydantic to test the DB CHECK constraint as defense-in-depth.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            backend.conn.execute(
                """
                INSERT INTO principal (id, kind, auth_method, trust_level, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("test", "human", "oidc", 11, "2025-01-01T00:00:00Z", "{}"),
            )

    def test_put_credential_and_resolve_by_token_hash(self, backend: SQLiteBackend) -> None:
        """Credential can be persisted and the principal resolved via its token hash."""
        credential = PrincipalCredential(
            id="cred-1",
            principal_id="alice@test.com",
            token_hash="deadbeef" * 8,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        backend.put_credential(credential)

        resolved = backend.get_principal_by_token_hash("deadbeef" * 8)
        assert resolved is not None
        assert resolved.id == "alice@test.com"

    def test_get_principal_by_unknown_token_hash_returns_none(self, backend: SQLiteBackend) -> None:
        assert backend.get_principal_by_token_hash("nonexistent") is None

    def test_revoked_credential_no_longer_resolves(self, backend: SQLiteBackend) -> None:
        credential = PrincipalCredential(
            id="cred-1",
            principal_id="alice@test.com",
            token_hash="deadbeef" * 8,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        backend.put_credential(credential)
        backend.revoke_credential("cred-1", datetime(2025, 1, 2, tzinfo=UTC), "admin@test.com")

        assert backend.get_principal_by_token_hash("deadbeef" * 8) is None

    def test_revoke_nonexistent_credential_raises(self, backend: SQLiteBackend) -> None:
        with pytest.raises(StorageError, match="Credential not found"):
            backend.revoke_credential(
                "nonexistent", datetime(2025, 1, 1, tzinfo=UTC), "admin@test.com"
            )

    def test_get_credential_by_id(self, backend: SQLiteBackend) -> None:
        credential = PrincipalCredential(
            id="cred-1",
            principal_id="alice@test.com",
            token_hash="deadbeef" * 8,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        backend.put_credential(credential)

        retrieved = backend.get_credential("cred-1")
        assert retrieved is not None
        assert retrieved.principal_id == "alice@test.com"
        assert retrieved.revoked_at is None

    def test_get_nonexistent_credential_returns_none(self, backend: SQLiteBackend) -> None:
        assert backend.get_credential("nonexistent") is None

    def test_get_credentials_for_principal_two_active_tokens(self, backend: SQLiteBackend) -> None:
        """A principal may hold multiple concurrent active credentials —
        revoking one doesn't invalidate the other."""
        backend.put_credential(
            PrincipalCredential(
                id="cred-1",
                principal_id="alice@test.com",
                token_hash="a" * 64,
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        backend.put_credential(
            PrincipalCredential(
                id="cred-2",
                principal_id="alice@test.com",
                token_hash="b" * 64,
                created_at=datetime(2025, 1, 2, tzinfo=UTC),
            )
        )

        credentials = backend.get_credentials_for_principal("alice@test.com")
        assert {c.id for c in credentials} == {"cred-1", "cred-2"}

        backend.revoke_credential("cred-1", datetime(2025, 1, 3, tzinfo=UTC), "admin@test.com")
        assert backend.get_principal_by_token_hash("a" * 64) is None
        assert backend.get_principal_by_token_hash("b" * 64) is not None

    def test_credential_issued_by_and_revoked_by_roundtrip(self, backend: SQLiteBackend) -> None:
        credential = PrincipalCredential(
            id="cred-1",
            principal_id="alice@test.com",
            token_hash="deadbeef" * 8,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            issued_by="admin@test.com",
        )
        backend.put_credential(credential)
        assert backend.get_credential("cred-1").issued_by == "admin@test.com"  # type: ignore[union-attr]
        assert backend.get_credential("cred-1").revoked_by is None  # type: ignore[union-attr]

        backend.revoke_credential("cred-1", datetime(2025, 1, 2, tzinfo=UTC), "carol@test.com")
        assert backend.get_credential("cred-1").revoked_by == "carol@test.com"  # type: ignore[union-attr]

    def test_re_revoking_credential_does_not_overwrite_revoker(
        self, backend: SQLiteBackend
    ) -> None:
        backend.put_credential(
            PrincipalCredential(
                id="cred-1",
                principal_id="alice@test.com",
                token_hash="deadbeef" * 8,
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        backend.revoke_credential("cred-1", datetime(2025, 1, 2, tzinfo=UTC), "admin@test.com")

        backend.revoke_credential("cred-1", datetime(2025, 1, 3, tzinfo=UTC), "carol@test.com")

        credential = backend.get_credential("cred-1")
        assert credential is not None
        assert credential.revoked_by == "admin@test.com"
        assert credential.revoked_at == datetime(2025, 1, 2, tzinfo=UTC)

    def test_put_and_get_admin_event(self, backend: SQLiteBackend) -> None:
        event = AdminEvent(
            id="event-1",
            actor="admin@test.com",
            action="create_principal",
            target="alice@test.com",
            at=datetime(2025, 1, 1, tzinfo=UTC),
            detail="bootstrap",
        )
        backend.put_admin_event(event)

        [retrieved] = backend.get_admin_events()
        assert retrieved == event

    def test_put_admin_event_duplicate_id_raises_storage_error(
        self, backend: SQLiteBackend
    ) -> None:
        """Covers put_admin_event's own IntegrityError -> StorageError
        wrapper, reached via a plain duplicate-id INSERT through the port
        (not a raw REPLACE statement against the connection directly)."""
        event = AdminEvent(
            id="event-1",
            actor="admin@test.com",
            action="create_principal",
            target="alice@test.com",
            at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        backend.put_admin_event(event)

        with pytest.raises(StorageError, match="conflict"):
            backend.put_admin_event(event)

    def test_get_admin_events_filters(self, backend: SQLiteBackend) -> None:
        backend.put_admin_event(
            AdminEvent(
                id="event-1",
                actor="admin@test.com",
                action="create_principal",
                target="alice@test.com",
                at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        backend.put_admin_event(
            AdminEvent(
                id="event-2",
                actor="carol@test.com",
                action="apply_schema",
                target="default:v1",
                at=datetime(2025, 1, 2, tzinfo=UTC),
            )
        )

        assert [e.id for e in backend.get_admin_events(actor="admin@test.com")] == ["event-1"]
        assert [e.id for e in backend.get_admin_events(target="default:v1")] == ["event-2"]
        assert [e.id for e in backend.get_admin_events()] == ["event-1", "event-2"]

    def test_get_admin_events_tiebreaks_same_timestamp_by_id(self, backend: SQLiteBackend) -> None:
        for event_id in ("event-b", "event-a"):
            backend.put_admin_event(
                AdminEvent(
                    id=event_id,
                    actor="admin@test.com",
                    action="create_principal",
                    target="alice@test.com",
                    at=datetime(2025, 1, 1, tzinfo=UTC),
                )
            )

        assert [e.id for e in backend.get_admin_events()] == ["event-a", "event-b"]

    def test_admin_event_update_rejected(self, backend: SQLiteBackend) -> None:
        backend.put_admin_event(
            AdminEvent(
                id="event-1",
                actor="admin@test.com",
                action="create_principal",
                target="alice@test.com",
                at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            backend.conn.execute(
                "UPDATE admin_event SET actor = 'mallory' WHERE id = ?", ("event-1",)
            )

    def test_admin_event_delete_rejected(self, backend: SQLiteBackend) -> None:
        backend.put_admin_event(
            AdminEvent(
                id="event-1",
                actor="admin@test.com",
                action="create_principal",
                target="alice@test.com",
                at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            backend.conn.execute("DELETE FROM admin_event WHERE id = ?", ("event-1",))

    def test_principal_credential_migration_adds_new_columns_to_existing_db(
        self, temp_db: Path
    ) -> None:
        """A database file created before issued_by/revoked_by existed
        gets them added on next open, without touching existing rows
        (KI-060) — simulates that by building the table in its pre-KI-060
        shape directly, bypassing SQLiteBackend's own (already-migrated)
        schema setup."""
        raw = sqlite3.connect(temp_db)
        raw.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, "
            "token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, revoked_at TEXT)"
        )
        raw.execute(
            "INSERT INTO principal_credential (id, principal_id, token_hash, created_at) "
            "VALUES ('old-cred', 'alice@test.com', 'oldhash', '2025-01-01T00:00:00+00:00')"
        )
        raw.commit()
        raw.close()

        backend = SQLiteBackend(temp_db)
        try:
            columns = {
                row[1] for row in backend.conn.execute("PRAGMA table_info(principal_credential)")
            }
            assert {"issued_by", "revoked_by"}.issubset(columns)
            old = backend.get_credential("old-cred")
            assert old is not None
            assert old.principal_id == "alice@test.com"
            assert old.issued_by is None
            assert old.revoked_by is None
        finally:
            backend.close()

    def test_proposal_reviewers_migration_adds_new_column_to_existing_db(
        self, temp_db: Path
    ) -> None:
        """A database file created before `reviewers` existed gets it added
        on next open (KI-078) — simulates that by building the table in its
        pre-KI-078 shape directly, bypassing SQLiteBackend's own
        (already-migrated) schema setup."""
        raw = sqlite3.connect(temp_db)
        raw.execute(
            "CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT NOT NULL, "
            "author TEXT NOT NULL, acting_as TEXT, state TEXT NOT NULL DEFAULT 'draft', "
            "created_at TEXT NOT NULL, decided_at TEXT, policy_reason TEXT, "
            "payload TEXT NOT NULL DEFAULT '{}', metadata TEXT NOT NULL DEFAULT '{}')"
        )
        raw.execute(
            "INSERT INTO proposal (id, namespace, author, state, created_at, policy_reason) "
            "VALUES ('old-prop', 'default', 'alice@test.com', 'require_review', "
            "'2025-01-01T00:00:00+00:00', 'needs review')"
        )
        raw.commit()
        raw.close()

        backend = SQLiteBackend(temp_db)
        try:
            columns = {row[1] for row in backend.conn.execute("PRAGMA table_info(proposal)")}
            assert "reviewers" in columns
            old = backend.get_proposal("old-prop")
            assert old is not None
            assert old.policy_reason == "needs review"
            assert old.reviewers == []
        finally:
            backend.close()

        # Migration must be idempotent - a second open of the now-migrated
        # file must not error (re-adding an already-present column) or
        # disturb an assignment made in between.
        backend2 = SQLiteBackend(temp_db)
        try:
            backend2.update_proposal_reviewers("old-prop", ["bob@test.com"])
        finally:
            backend2.close()
        backend3 = SQLiteBackend(temp_db)
        try:
            reopened = backend3.get_proposal("old-prop")
            assert reopened is not None
            assert reopened.reviewers == ["bob@test.com"]
        finally:
            backend3.close()

    def _put_proposal(self, backend: SQLiteBackend, proposal_id: str) -> None:
        backend.put_proposal(
            Proposal(
                id=proposal_id,
                namespace="default",
                author="alice@test.com",
                state="require_review",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )

    def test_proposals_filter_by_state(self, backend: SQLiteBackend) -> None:
        self._put_proposal(backend, "prop-pending")
        backend.put_proposal(
            Proposal(
                id="prop-accepted",
                namespace="default",
                author="alice@test.com",
                state="accepted",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )

        assert [p.id for p in backend.proposals(state="require_review")] == ["prop-pending"]
        assert {p.id for p in backend.proposals()} == {"prop-pending", "prop-accepted"}
        assert backend.proposals(state="rejected") == []

    def test_proposals_ordered_most_recently_created_first(self, backend: SQLiteBackend) -> None:
        backend.put_proposal(
            Proposal(
                id="prop-older",
                namespace="default",
                author="alice@test.com",
                state="require_review",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        backend.put_proposal(
            Proposal(
                id="prop-newer",
                namespace="default",
                author="alice@test.com",
                state="require_review",
                created_at=datetime(2025, 1, 2, tzinfo=UTC),
            )
        )

        assert [p.id for p in backend.proposals()] == ["prop-newer", "prop-older"]

    def test_put_and_get_proposal_events(self, backend: SQLiteBackend) -> None:
        self._put_proposal(backend, "prop-1")
        backend.put_proposal_event(
            ProposalEvent(
                id="event-1",
                proposal_id="prop-1",
                actor="alice@test.com",
                type="accept",
                at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )

        events = backend.get_proposal_events("prop-1")
        assert len(events) == 1
        assert events[0].id == "event-1"
        assert events[0].type == "accept"
        assert events[0].actor == "alice@test.com"
        assert events[0].detail is None

    def test_get_proposal_events_ordered_oldest_first(self, backend: SQLiteBackend) -> None:
        self._put_proposal(backend, "prop-1")
        backend.put_proposal_event(
            ProposalEvent(
                id="event-1",
                proposal_id="prop-1",
                actor="alice@test.com",
                type="reject",
                detail="first pass",
                at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        backend.put_proposal_event(
            ProposalEvent(
                id="event-2",
                proposal_id="prop-1",
                actor="alice@test.com",
                type="accept",
                at=datetime(2025, 1, 2, tzinfo=UTC),
            )
        )

        events = backend.get_proposal_events("prop-1")
        assert [e.id for e in events] == ["event-1", "event-2"]

    def test_get_proposal_events_empty_for_unknown_proposal(self, backend: SQLiteBackend) -> None:
        assert backend.get_proposal_events("nonexistent") == []

    def test_proposal_events_scoped_to_their_own_proposal(self, backend: SQLiteBackend) -> None:
        self._put_proposal(backend, "prop-1")
        self._put_proposal(backend, "prop-2")
        backend.put_proposal_event(
            ProposalEvent(
                id="event-1",
                proposal_id="prop-1",
                actor="alice@test.com",
                type="accept",
                at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        backend.put_proposal_event(
            ProposalEvent(
                id="event-2",
                proposal_id="prop-2",
                actor="alice@test.com",
                type="reject",
                at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )

        assert [e.id for e in backend.get_proposal_events("prop-1")] == ["event-1"]
        assert [e.id for e in backend.get_proposal_events("prop-2")] == ["event-2"]

    def test_update_proposal_state_none_reason_preserves_existing(
        self, backend: SQLiteBackend
    ) -> None:
        """policy_reason=None leaves the stored value unchanged (COALESCE) -
        review actions must not clobber the policy engine's original reason."""
        backend.put_proposal(
            Proposal(
                id="prop-1",
                namespace="default",
                author="alice@test.com",
                state="require_review",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                policy_reason="AI proposals require review",
            )
        )

        backend.update_proposal_state("prop-1", "accepted", datetime(2025, 1, 2).isoformat())

        updated = backend.get_proposal("prop-1")
        assert updated is not None
        assert updated.state == "accepted"
        assert updated.policy_reason == "AI proposals require review"

    def test_update_proposal_reviewers_replaces_wholesale(self, backend: SQLiteBackend) -> None:
        """Unlike policy_reason's COALESCE, reviewers is always replaced
        with what's passed - including an empty list (KI-078)."""
        backend.put_proposal(
            Proposal(
                id="prop-1",
                namespace="default",
                author="alice@test.com",
                state="require_review",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                reviewers=["bob@test.com"],
            )
        )

        backend.update_proposal_reviewers("prop-1", ["carol@test.com", "dave@test.com"])
        updated = backend.get_proposal("prop-1")
        assert updated is not None
        assert updated.reviewers == ["carol@test.com", "dave@test.com"]

        backend.update_proposal_reviewers("prop-1", [])
        cleared = backend.get_proposal("prop-1")
        assert cleared is not None
        assert cleared.reviewers == []

    def test_update_proposal_reviewers_nonexistent_raises(self, backend: SQLiteBackend) -> None:
        with pytest.raises(StorageError, match="not found"):
            backend.update_proposal_reviewers("nonexistent", ["alice@test.com"])

    def test_put_and_get_schema(self, backend: SQLiteBackend) -> None:
        """Schema can be persisted and retrieved."""
        schema = SchemaIR(
            namespace="test-ns",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text"),
                    },
                ),
            },
        )

        backend.put_schema(schema)
        retrieved = backend.get_schema("test-ns", version=1)

        assert retrieved is not None
        assert retrieved.namespace == schema.namespace
        assert retrieved.version == schema.version
        assert "Person" in retrieved.concepts

    def test_put_schema_registers_its_namespace(self, backend: SQLiteBackend) -> None:
        """A namespace that only ever has a schema applied - never an
        entity - is still discoverable via list_namespaces() (KI-022):
        the exact blind spot a SELECT DISTINCT-over-entity approach would
        have had."""
        schema = SchemaIR(namespace="acme-research", version=1, concepts={})

        backend.put_schema(schema)

        assert {n.id for n in backend.list_namespaces()} == {"default", "acme-research"}

    def test_get_latest_schema_version(self, backend: SQLiteBackend) -> None:
        """Getting schema without version returns latest."""
        schema_v1 = SchemaIR(namespace="test-ns", version=1)
        schema_v2 = SchemaIR(namespace="test-ns", version=2)

        backend.put_schema(schema_v1)
        backend.put_schema(schema_v2)

        latest = backend.get_schema("test-ns")
        assert latest is not None
        assert latest.version == 2

    def test_get_nonexistent_schema_returns_none(self, backend: SQLiteBackend) -> None:
        """Getting a nonexistent schema returns None."""
        assert backend.get_schema("nonexistent") is None

    def test_get_schema_at_resolves_effective_version(self, temp_db: Path) -> None:
        """KI-019: get_schema_at resolves the version effective at a point in
        time (via the applied_at each put_schema already records), not
        always the latest version."""
        clock = FixedClock(datetime(2025, 1, 1, tzinfo=UTC))
        backend = SQLiteBackend(temp_db, clock=clock)
        backend.put_schema(SchemaIR(namespace="test-ns", version=1))

        clock.set(datetime(2025, 6, 1, tzinfo=UTC))
        backend.put_schema(SchemaIR(namespace="test-ns", version=2))

        early = backend.get_schema_at("test-ns", datetime(2025, 1, 1, tzinfo=UTC))
        assert early is not None
        assert early.version == 1

        late = backend.get_schema_at("test-ns", datetime(2025, 6, 1, tzinfo=UTC))
        assert late is not None
        assert late.version == 2

        assert backend.get_schema_at("test-ns", datetime(2024, 12, 31, tzinfo=UTC)) is None

    def test_ai_principal_without_owner_rejected_by_db(self, backend: SQLiteBackend) -> None:
        """AI principal without owner is rejected by DB CHECK constraint (defense-in-depth)."""
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            backend.conn.execute(
                """
                INSERT INTO principal (id, kind, auth_method, trust_level, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("bot-no-owner", "ai", "workload", 0, "2025-01-01T00:00:00Z", "{}"),
            )

    def test_transaction_context_manager_commits(self, backend: SQLiteBackend) -> None:
        """transaction() context manager commits on success."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        with backend.transaction():
            backend.put_entity(entity)

        assert backend.get_entity("entity-001") is not None

    def test_transaction_context_manager_rolls_back_on_error(self, backend: SQLiteBackend) -> None:
        """transaction() context manager rolls back on exception."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        with pytest.raises(ValueError):
            with backend.transaction():
                backend.put_entity(entity)
                raise ValueError("simulated failure")

        assert backend.get_entity("entity-001") is None

    def test_writes_survive_connection_close(self, temp_db: Path) -> None:
        """Data written via one connection is visible after close and reopen (ADR-0010)."""
        b1 = SQLiteBackend(temp_db)
        b1.put_principal(
            Principal(
                id="alice@test.com",
                kind="human",
                auth_method="oidc",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        b1.put_entity(entity)
        b1.close()

        b2 = SQLiteBackend(temp_db)
        assert b2.get_entity("entity-001") is not None
        b2.close()

    def test_put_principal_inside_transaction_skips_autocommit(
        self, backend: SQLiteBackend
    ) -> None:
        """put_principal inside a transaction does not auto-commit (ADR-0010)."""
        principal = Principal(
            id="bob@test.com",
            kind="human",
            auth_method="oidc",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        with backend.transaction():
            backend.put_principal(principal)
        assert backend.get_principal("bob@test.com") is not None

    def test_put_assertion_inside_transaction_skips_autocommit(
        self, backend: SQLiteBackend
    ) -> None:
        """put_assertion inside a transaction does not auto-commit (ADR-0010)."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)
        assertion = Assertion(
            id="assertion-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        with backend.transaction():
            backend.put_assertion(assertion)
        assert len(backend.assertions(subject="entity-001")) == 1

    def test_set_assertion_status_inside_transaction_skips_autocommit(
        self, backend: SQLiteBackend
    ) -> None:
        """set_assertion_status inside a transaction does not auto-commit (ADR-0010)."""
        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(entity)
        assertion = Assertion(
            id="assertion-001",
            namespace="test-ns",
            subject="entity-001",
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author="alice@test.com",
            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        backend.put_assertion(assertion)
        with backend.transaction():
            backend.set_assertion_status("assertion-001", "retracted")
        assert backend.assertions(subject="entity-001") == []

    def test_put_schema_inside_transaction_skips_autocommit(self, backend: SQLiteBackend) -> None:
        """put_schema inside a transaction does not auto-commit (ADR-0010)."""
        schema = SchemaIR(namespace="test-ns", version=1)
        with backend.transaction():
            backend.put_schema(schema)
        assert backend.get_schema("test-ns") is not None

    def test_entities_filter_by_namespace_only(self, backend: SQLiteBackend) -> None:
        """entities() filtered by namespace only returns all concepts in that namespace."""
        e1 = Entity(
            id="entity-001",
            namespace="ns-a",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        e2 = Entity(
            id="entity-002",
            namespace="ns-a",
            concept="Organization",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        e3 = Entity(
            id="entity-003",
            namespace="ns-b",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )
        backend.put_entity(e1)
        backend.put_entity(e2)
        backend.put_entity(e3)

        results = backend.entities(namespace="ns-a")
        assert len(results) == 2
        assert {r.id for r in results} == {"entity-001", "entity-002"}

    def test_entities_no_filters_returns_all(self, backend: SQLiteBackend) -> None:
        """entities() with no filters returns all entities."""
        for i in range(3):
            backend.put_entity(
                Entity(
                    id=f"entity-{i:03d}",
                    namespace="test-ns",
                    concept="Person",
                    created_at=datetime(2025, 1, 1, tzinfo=UTC),
                    created_by="alice@test.com",
                )
            )
        assert len(backend.entities()) == 3

    def test_entities_where_matches_relation_target_id(self, backend: SQLiteBackend) -> None:
        """entities_where() matches a relation filter against value_ref, not just value_lit (KI-030)."""
        for entity_id in ("person-001", "org-001"):
            backend.put_entity(
                Entity(
                    id=entity_id,
                    namespace="test-ns",
                    concept="Person" if entity_id.startswith("person") else "Organization",
                    created_at=datetime(2025, 1, 1, tzinfo=UTC),
                    created_by="alice@test.com",
                )
            )
        backend.put_assertion(
            Assertion(
                id="assertion-001",
                namespace="test-ns",
                subject="person-001",
                predicate="Person.employer",
                value_kind="ref",
                value="org-001",
                author="alice@test.com",
                asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )

        results = backend.entities_where(
            "test-ns", "Person", [("Person.employer", "eq", "org-001")]
        )
        assert [r.id for r in results] == ["person-001"]

        no_match = backend.entities_where(
            "test-ns", "Person", [("Person.employer", "eq", "org-002")]
        )
        assert no_match == []

    def test_entities_where_status_widening_flags(self, backend: SQLiteBackend) -> None:
        """KI-081: current-state entities_where() matches 'active' only by
        default; include_flagged / include_history widen it, independently."""
        backend.put_entity(
            Entity(
                id="p1",
                namespace="test-ns",
                concept="Person",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                created_by="alice@test.com",
            )
        )
        for aid, status in (
            ("a-flagged", "flagged"),
            ("a-super", "superseded"),
            ("a-retr", "retracted"),
        ):
            backend.put_assertion(
                Assertion(
                    id=aid,
                    namespace="test-ns",
                    subject="p1",
                    predicate="Person.tag",
                    value_kind="literal",
                    value_type="Text",
                    value=aid,
                    author="alice@test.com",
                    asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
                    status=status,
                )
            )

        def match(value: str, **kw: bool) -> list[str]:
            return [
                e.id
                for e in backend.entities_where(
                    "test-ns", "Person", [("Person.tag", "eq", value)], **kw
                )
            ]

        assert match("a-flagged") == []  # excluded by default
        assert match("a-flagged", include_flagged=True) == ["p1"]
        assert match("a-flagged", include_history=True) == []  # history != flagged
        assert match("a-super", include_history=True) == ["p1"]
        assert match("a-retr", include_history=True) == ["p1"]
        assert match("a-super", include_flagged=True) == []  # flagged != history
        assert match("a-super", include_flagged=True, include_history=True) == ["p1"]

    def test_contradictions_filter_by_state(self, backend: SQLiteBackend) -> None:
        backend.put_contradiction(
            Contradiction(
                id="contra-open",
                namespace="default",
                subject="entity-001",
                predicate="Person.name",
                member_ids=["a-1", "a-2"],
                state="open",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        backend.put_contradiction(
            Contradiction(
                id="contra-resolved",
                namespace="default",
                subject="entity-002",
                predicate="Person.name",
                member_ids=["a-3", "a-4"],
                state="resolved",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                resolved_by="alice@test.com",
                resolved_at=datetime(2025, 1, 2, tzinfo=UTC),
            )
        )

        assert [c.id for c in backend.contradictions(state="open")] == ["contra-open"]
        assert {c.id for c in backend.contradictions()} == {"contra-open", "contra-resolved"}
        assert backend.contradictions(state="resolved")[0].id == "contra-resolved"

    def test_update_contradiction_members_metadata_param(self, backend: SQLiteBackend) -> None:
        """KI-071: update_contradiction_members's new `metadata` param
        replaces the metadata blob wholesale when given, and leaves it
        untouched (not wiped) when omitted."""
        backend.put_contradiction(
            Contradiction(
                id="contra-meta",
                namespace="default",
                subject="entity-001",
                predicate="Person.name",
                member_ids=["a-1", "a-2"],
                state="open",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                metadata={"rationale_history": [{"rationale": "first"}]},
            )
        )

        backend.update_contradiction_members("contra-meta", ["a-1", "a-2", "a-3"])
        untouched = backend.get_contradiction("contra-meta")
        assert untouched is not None
        assert untouched.member_ids == ["a-1", "a-2", "a-3"]
        assert untouched.metadata == {"rationale_history": [{"rationale": "first"}]}

        backend.update_contradiction_members(
            "contra-meta",
            ["a-1", "a-2", "a-3", "a-4"],
            metadata={"rationale_history": ["replaced"]},
        )
        replaced = backend.get_contradiction("contra-meta")
        assert replaced is not None
        assert replaced.member_ids == ["a-1", "a-2", "a-3", "a-4"]
        assert replaced.metadata == {"rationale_history": ["replaced"]}

    def test_contradictions_ordered_most_recently_created_first(
        self, backend: SQLiteBackend
    ) -> None:
        backend.put_contradiction(
            Contradiction(
                id="contra-older",
                namespace="default",
                subject="entity-001",
                predicate="Person.name",
                member_ids=["a-1", "a-2"],
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        backend.put_contradiction(
            Contradiction(
                id="contra-newer",
                namespace="default",
                subject="entity-002",
                predicate="Person.name",
                member_ids=["a-3", "a-4"],
                created_at=datetime(2025, 1, 2, tzinfo=UTC),
            )
        )

        assert [c.id for c in backend.contradictions()] == ["contra-newer", "contra-older"]

    def test_vector_table_created_lazily_on_first_upsert(self, backend: SQLiteBackend) -> None:
        """vector_{scope} is only created once a vector is actually upserted."""
        cursor = backend.conn.cursor()
        before = cursor.execute(
            "SELECT name FROM sqlite_master WHERE name = 'vector_entity'"
        ).fetchall()
        assert before == []

        backend.vector_upsert("entity", "e1", [1.0, 0.0])

        after = cursor.execute(
            "SELECT name FROM sqlite_master WHERE name = 'vector_entity'"
        ).fetchall()
        assert len(after) == 1

    def test_load_extension_disabled_after_init(self, backend: SQLiteBackend) -> None:
        """enable_load_extension is toggled back off after loading sqlite-vec (supply-chain hygiene)."""
        with pytest.raises(sqlite3.OperationalError, match="not authorized"):
            backend.conn.load_extension("vec0")

    def test_case_sensitive_like_pragma_is_set(self, backend: SQLiteBackend) -> None:
        """PRAGMA case_sensitive_like = ON is set at connection time (KI-039)
        so entities_where()'s __contains operator matches DuckDB's
        case-sensitive LIKE default, rather than SQLite's own
        case-insensitive one."""
        # PRAGMA case_sensitive_like has no query form reporting the
        # current setting - it's write-only - so this asserts the effect
        # instead: 'Ada' LIKE 'ada' would match if case-insensitive.
        assert backend.conn.execute("SELECT 'Ada' LIKE 'ada'").fetchone()[0] == 0

    def test_range_operator_on_non_numeric_stored_value(self, backend: SQLiteBackend) -> None:
        """KI-049: nothing validates that value_lit's actual content
        parses as the predicate's declared value_type - a row with
        non-numeric text stored against a nominally-numeric predicate
        CASTs to 0.0 on SQLite (no TRY_CAST available), a documented,
        tracked divergence from DuckDB's TRY_CAST-based exclusion (see
        the DuckDB backend's own equivalent test)."""
        backend.put_entity(
            Entity(
                id="e0",
                namespace="test-ns",
                concept="Person",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                created_by="alice@test.com",
            )
        )
        backend.put_assertion(
            Assertion(
                id="a0",
                namespace="test-ns",
                subject="e0",
                predicate="Person.age",
                value_kind="literal",
                value_type="Integer",
                value="unknown",
                author="alice@test.com",
                asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )

        matches_below = backend.entities_where("test-ns", "Person", [("Person.age", "lt", 10.0)])
        assert [e.id for e in matches_below] == ["e0"]

        matches_above = backend.entities_where("test-ns", "Person", [("Person.age", "gt", 10.0)])
        assert matches_above == []


class TestCandidateIdsNarrowing:
    """KI-037: entities_meeting_confidence/entities_meeting_trust narrow the
    (namespace, concept) scan to an optional candidate_ids hint via a
    single JSON-encoded bound parameter, rather than always scanning the
    full concept. SQLite-specific: pins the actual narrowing behavior via
    this backend's own json_each-based implementation; DuckDBBackend's
    identically-shaped unnest()-based implementation has its own mirror of
    this class in tests/unit/test_duckdb_backend.py. Cross-backend
    correctness that holds regardless of which (or whether a) narrowing
    strategy a given backend uses lives in
    conformance/test_confidence_trust_filters.py instead."""

    def _seed(self, backend: SQLiteBackend, entity_id: str, *, confidence: float | None) -> None:
        backend.put_entity(
            Entity(
                id=entity_id,
                namespace="test-ns",
                concept="Person",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                created_by="alice@test.com",
            )
        )
        backend.put_assertion(
            Assertion(
                id=f"a-{entity_id}",
                namespace="test-ns",
                subject=entity_id,
                predicate="Person.name",
                value_kind="literal",
                value_type="Text",
                value="Ada",
                author="alice@test.com",
                confidence=confidence,
                asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )

    def test_entities_meeting_confidence_narrows_to_candidate_ids(
        self, backend: SQLiteBackend
    ) -> None:
        self._seed(backend, "e0", confidence=0.9)
        self._seed(backend, "e1", confidence=0.9)

        assert backend.entities_meeting_confidence("test-ns", "Person", 0.5) == {"e0", "e1"}
        narrowed = backend.entities_meeting_confidence(
            "test-ns", "Person", 0.5, candidate_ids=frozenset({"e0"})
        )
        assert narrowed == {"e0"}

    def test_entities_meeting_trust_narrows_to_candidate_ids(self, backend: SQLiteBackend) -> None:
        self._seed(backend, "e0", confidence=None)
        self._seed(backend, "e1", confidence=None)

        # alice@test.com defaults to trust_level=0 (ADR-0009); min_trust=0 qualifies both.
        assert backend.entities_meeting_trust("test-ns", "Person", 0) == {"e0", "e1"}
        narrowed = backend.entities_meeting_trust(
            "test-ns", "Person", 0, candidate_ids=frozenset({"e0"})
        )
        assert narrowed == {"e0"}

    def test_entities_meeting_confidence_empty_candidate_ids_short_circuits(
        self, backend: SQLiteBackend
    ) -> None:
        """An empty (not None) candidate_ids means "nothing to narrow to" -
        must return empty rather than erroring on an empty JSON array or
        (worse) silently falling back to the unnarrowed scan. `e0` would
        qualify concept-wide (confidence=0.9 >= 0.5); asserting it's absent
        here proves the empty set was actually honored."""
        self._seed(backend, "e0", confidence=0.9)

        assert (
            backend.entities_meeting_confidence("test-ns", "Person", 0.5, candidate_ids=frozenset())
            == set()
        )

    def test_entities_meeting_trust_empty_candidate_ids_short_circuits(
        self, backend: SQLiteBackend
    ) -> None:
        self._seed(backend, "e0", confidence=None)

        assert (
            backend.entities_meeting_trust("test-ns", "Person", 0, candidate_ids=frozenset())
            == set()
        )

    def _seed_with_status(self, backend: SQLiteBackend, entity_id: str, status: str) -> None:
        backend.put_entity(
            Entity(
                id=entity_id,
                namespace="test-ns",
                concept="Person",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                created_by="alice@test.com",
            )
        )
        backend.put_assertion(
            Assertion(
                id=f"a-{entity_id}",
                namespace="test-ns",
                subject=entity_id,
                predicate="Person.name",
                value_kind="literal",
                value_type="Text",
                value="Ada",
                author="alice@test.com",
                confidence=0.9,
                asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
                status=status,
            )
        )

    def test_entities_meeting_confidence_status_widening_flags(
        self, backend: SQLiteBackend
    ) -> None:
        """KI-093: entities_meeting_confidence()'s current-state path must
        widen the same way entities_where() does, independently per flag."""
        self._seed_with_status(backend, "e-flagged", "flagged")
        self._seed_with_status(backend, "e-super", "superseded")
        self._seed_with_status(backend, "e-retr", "retracted")

        def qualifying(**kw: bool) -> set[str]:
            return backend.entities_meeting_confidence("test-ns", "Person", 0.5, **kw)

        assert qualifying() == set()
        assert qualifying(include_flagged=True) == {"e-flagged"}
        assert qualifying(include_history=True) == {"e-super", "e-retr"}
        assert qualifying(include_flagged=True, include_history=True) == {
            "e-flagged",
            "e-super",
            "e-retr",
        }

    def test_entities_meeting_trust_status_widening_flags(self, backend: SQLiteBackend) -> None:
        """KI-093: entities_meeting_trust()'s current-state path must widen
        the same way entities_where() does, independently per flag."""
        self._seed_with_status(backend, "e-flagged", "flagged")
        self._seed_with_status(backend, "e-super", "superseded")

        def qualifying(**kw: bool) -> set[str]:
            return backend.entities_meeting_trust("test-ns", "Person", 0, **kw)

        assert qualifying() == set()
        assert qualifying(include_flagged=True) == {"e-flagged"}
        assert qualifying(include_history=True) == {"e-super"}
        assert qualifying(include_flagged=True, include_history=True) == {"e-flagged", "e-super"}

    def _seed_flagged_with_window(self, backend: SQLiteBackend, entity_id: str) -> None:
        """A flagged assertion whose validity window covers T0 (2025-01-01),
        for `as_of_time`-scoped widening tests."""
        backend.put_entity(
            Entity(
                id=entity_id,
                namespace="test-ns",
                concept="Person",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                created_by="alice@test.com",
            )
        )
        backend.put_assertion(
            Assertion(
                id=f"a-{entity_id}",
                namespace="test-ns",
                subject=entity_id,
                predicate="Person.name",
                value_kind="literal",
                value_type="Text",
                value="Ada",
                author="alice@test.com",
                confidence=0.9,
                asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
                valid_from=datetime(2025, 1, 1, tzinfo=UTC),
                status="flagged",
            )
        )

    def test_entities_meeting_confidence_as_of_include_flagged(
        self, backend: SQLiteBackend
    ) -> None:
        """KI-093: the as_of branch's flagged_clause must be genuinely
        conditional on include_flagged, not left unconditionally excluding
        flagged the way it did before this KI - a mutation reverting that
        one line survived the whole suite until this test was added."""
        self._seed_flagged_with_window(backend, "e-flagged")
        t = datetime(2025, 6, 1, tzinfo=UTC)

        assert backend.entities_meeting_confidence("test-ns", "Person", 0.5, as_of_time=t) == set()
        assert backend.entities_meeting_confidence(
            "test-ns", "Person", 0.5, as_of_time=t, include_flagged=True
        ) == {"e-flagged"}

    def test_entities_meeting_trust_as_of_include_flagged(self, backend: SQLiteBackend) -> None:
        """KI-093: same as the confidence test above, for entities_meeting_trust()."""
        self._seed_flagged_with_window(backend, "e-flagged")
        t = datetime(2025, 6, 1, tzinfo=UTC)

        assert backend.entities_meeting_trust("test-ns", "Person", 0, as_of_time=t) == set()
        assert backend.entities_meeting_trust(
            "test-ns", "Person", 0, as_of_time=t, include_flagged=True
        ) == {"e-flagged"}


class TestConcurrency:
    """KI-023: the shared connection must survive genuinely concurrent access
    from multiple threads (the shape an ASGI server's worker threadpool
    produces), not just serial or single-threaded use.
    """

    N_THREADS = 8

    def test_concurrent_transaction_blocks_survive_and_all_commit(
        self, backend: SQLiteBackend
    ) -> None:
        """N threads each run a full transaction() block concurrently.

        Before the fix, a second begin() while another thread's transaction
        was still open raised a raw (unmapped) sqlite3.OperationalError —
        the exact scenario KI-023 describes. With the lock, each thread's
        transaction is fully serialized: no exception, and every write
        survives.
        """
        barrier = threading.Barrier(self.N_THREADS)
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            barrier.wait()  # maximize actual concurrent contention on begin()
            try:
                with backend.transaction():
                    backend.put_entity(
                        Entity(
                            id=f"entity-{i}",
                            namespace="test-ns",
                            concept="Person",
                            created_at=datetime(2025, 1, 1, tzinfo=UTC),
                            created_by="alice@test.com",
                        )
                    )
                    backend.put_assertion(
                        Assertion(
                            id=f"assertion-{i}",
                            namespace="test-ns",
                            subject=f"entity-{i}",
                            predicate="Person.name",
                            value_kind="literal",
                            value_type="Text",
                            value=f"Person {i}",
                            author="alice@test.com",
                            asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
                        )
                    )
            except BaseException as e:  # noqa: BLE001 - captured for the assertion below
                errors.append(e)

        with ThreadPoolExecutor(max_workers=self.N_THREADS) as pool:
            list(pool.map(worker, range(self.N_THREADS)))

        assert errors == []
        for i in range(self.N_THREADS):
            assert backend.get_entity(f"entity-{i}") is not None
            assert backend.get_assertion(f"assertion-{i}") is not None

    def test_concurrent_non_transactional_writes_are_serialized(
        self, backend: SQLiteBackend
    ) -> None:
        """N threads each call put_entity() directly (autocommit path, no
        explicit transaction()) concurrently — exercises the @_synchronized
        wrapper on a standalone write, not just the begin()/commit() path.
        """
        barrier = threading.Barrier(self.N_THREADS)
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            barrier.wait()
            try:
                backend.put_entity(
                    Entity(
                        id=f"solo-entity-{i}",
                        namespace="test-ns",
                        concept="Person",
                        created_at=datetime(2025, 1, 1, tzinfo=UTC),
                        created_by="alice@test.com",
                    )
                )
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        with ThreadPoolExecutor(max_workers=self.N_THREADS) as pool:
            list(pool.map(worker, range(self.N_THREADS)))

        assert errors == []
        entities = backend.entities(namespace="test-ns", concept="Person")
        assert {e.id for e in entities} == {f"solo-entity-{i}" for i in range(self.N_THREADS)}

    @pytest.mark.parametrize(
        "call_reader",
        [
            lambda backend: backend.entities_meeting_confidence("test-ns", "Person", 0.5),
            lambda backend: backend.entities_meeting_trust("test-ns", "Person", 0),
        ],
        ids=["entities_meeting_confidence", "entities_meeting_trust"],
    )
    def test_entities_meeting_confidence_or_trust_serialized_with_open_transaction(
        self, backend: SQLiteBackend, call_reader: Callable[[SQLiteBackend], set[str]]
    ) -> None:
        """entities_meeting_confidence/entities_meeting_trust must carry
        @_synchronized like every other public method (KI-023) - a
        concurrent call must block on an in-flight transaction rather than
        dirty-reading its uncommitted write. The writer's transaction is
        forced to roll back after the reader unblocks, so a passing
        assertion of `set()` proves the reader waited for the lock rather
        than observing (and returning) the uncommitted row. Parametrized
        over both methods - a decorator missing from just one of them still
        leaves the suite green if only the other is exercised."""
        backend.put_entity(
            Entity(
                id="e0",
                namespace="test-ns",
                concept="Person",
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
                created_by="alice@test.com",
            )
        )

        entered = threading.Event()
        release = threading.Event()

        def writer() -> None:
            with backend.transaction():
                backend.put_assertion(
                    Assertion(
                        id="a0",
                        namespace="test-ns",
                        subject="e0",
                        predicate="Person.name",
                        value_kind="literal",
                        value_type="Text",
                        value="Ada",
                        author="alice@test.com",
                        confidence=0.9,
                        asserted_at=datetime(2025, 1, 1, tzinfo=UTC),
                    )
                )
                entered.set()
                release.wait(timeout=5)
                raise RuntimeError("forced rollback")

        result: list[set[str]] = []

        def reader() -> None:
            entered.wait(timeout=5)
            result.append(call_reader(backend))

        with ThreadPoolExecutor(max_workers=2) as pool:
            writer_future = pool.submit(writer)
            reader_future = pool.submit(reader)
            try:
                assert entered.wait(timeout=5)
                time.sleep(0.1)
                assert not reader_future.done()  # still blocked on the writer's lock
            finally:
                # Always unblock the writer's release.wait(), even if an
                # assertion above failed - otherwise ThreadPoolExecutor's
                # __exit__ blocks for the writer's full 5s timeout on every
                # failing run.
                release.set()
            with pytest.raises(RuntimeError, match="forced rollback"):
                writer_future.result(timeout=5)
            reader_future.result(timeout=5)

        assert result == [set()]

    def test_commit_failure_inside_transaction_raises_storage_error_and_frees_lock(
        self, backend: SQLiteBackend
    ) -> None:
        """Regression test for a lock double-release found reviewing KI-023.

        commit() releasing self._lock unconditionally (e.g. via `finally`)
        double-releases it when commit() fails inside a transaction() block,
        since transaction()'s except clause then calls rollback(), which
        also releases — the second release raises RuntimeError, masking the
        real StorageError and leaving _in_transaction stuck True forever
        (self._lock itself is not left deadlocked, since a release on an
        already-free RLock raises rather than corrupting lock state, but
        the masked error and stuck flag are still a real regression).
        """

        class _FailingCommitConn:
            """Delegates everything to the real connection except commit()."""

            def __init__(self, real_conn: sqlite3.Connection) -> None:
                self._real = real_conn

            def commit(self) -> None:
                raise sqlite3.OperationalError("simulated commit failure")

            def __getattr__(self, name: str) -> object:
                return getattr(self._real, name)

        entity = Entity(
            id="entity-001",
            namespace="test-ns",
            concept="Person",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            created_by="alice@test.com",
        )

        real_conn = backend.conn
        backend.conn = _FailingCommitConn(real_conn)  # type: ignore[assignment]
        try:
            with pytest.raises(StorageError, match="Failed to commit transaction"):
                with backend.transaction():
                    backend.put_entity(entity)
        finally:
            backend.conn = real_conn

        assert backend._in_transaction is False
        assert backend.get_entity("entity-001") is None  # rollback() undid the insert

        # The lock must be free — acquire(blocking=False) succeeds only if so.
        assert backend._lock.acquire(blocking=False)
        backend._lock.release()

    def test_begin_reports_generic_message_for_non_busy_operational_error(
        self, backend: SQLiteBackend
    ) -> None:
        """KI-084: begin()'s SQLITE_BUSY-family masking must not swallow
        an unrelated OperationalError (e.g. disk I/O) into the same "safe
        to retry" message — that would mischaracterize a genuine fault as
        transient. A real non-BUSY OperationalError is hard to provoke
        deterministically from Python, so this substitutes the connection
        with a stand-in raising a synthetic one carrying a non-BUSY
        extended error code, mirroring the `_FailingCommitConn` pattern
        used elsewhere in this file for the same reason.
        """

        class _FailingBeginConn:
            """Delegates everything to the real connection except execute()."""

            def __init__(self, real_conn: sqlite3.Connection) -> None:
                self._real = real_conn

            def execute(self, sql: str, *args: object) -> object:
                if sql == "BEGIN IMMEDIATE":
                    err = sqlite3.OperationalError("disk I/O error")
                    err.sqlite_errorcode = sqlite3.SQLITE_IOERR  # not SQLITE_BUSY
                    raise err
                return self._real.execute(sql, *args)

            def __getattr__(self, name: str) -> object:
                return getattr(self._real, name)

        real_conn = backend.conn
        backend.conn = _FailingBeginConn(real_conn)  # type: ignore[assignment]
        try:
            with pytest.raises(StorageError, match="Failed to begin transaction") as exc_info:
                backend.begin()
            assert "safe to retry" not in str(exc_info.value)
        finally:
            backend.conn = real_conn

        # The lock must be free — begin()'s non-BUSY branch releases it too.
        assert backend._lock.acquire(blocking=False)
        backend._lock.release()

    def test_begin_masks_extended_busy_variant_as_retryable(self, backend: SQLiteBackend) -> None:
        """KI-084 review: the sibling test above alone leaves `code & 0xFF
        == sqlite3.SQLITE_BUSY` indistinguishable from a plain `code ==
        sqlite3.SQLITE_BUSY` — at the real cross-process contention site,
        `BEGIN IMMEDIATE` means only literal `SQLITE_BUSY` (5) is ever
        actually observed, so a masking regression there wouldn't be
        caught by exercising that site alone. This injects a synthetic
        `SQLITE_BUSY_SNAPSHOT` (517 — an extended code sharing primary
        code 5 in its low byte) via the same connection stand-in, proving
        the `& 0xFF` masking specifically, not just the BUSY/non-BUSY
        split: a plain `code == sqlite3.SQLITE_BUSY` equality check would
        wrongly fall through to the generic message here.
        """

        class _FailingBeginConn:
            """Delegates everything to the real connection except execute()."""

            def __init__(self, real_conn: sqlite3.Connection) -> None:
                self._real = real_conn

            def execute(self, sql: str, *args: object) -> object:
                if sql == "BEGIN IMMEDIATE":
                    err = sqlite3.OperationalError("database is locked")
                    err.sqlite_errorcode = sqlite3.SQLITE_BUSY_SNAPSHOT  # 517, not 5
                    raise err
                return self._real.execute(sql, *args)

            def __getattr__(self, name: str) -> object:
                return getattr(self._real, name)

        real_conn = backend.conn
        backend.conn = _FailingBeginConn(real_conn)  # type: ignore[assignment]
        try:
            with pytest.raises(StorageError, match="lock contention.*safe to retry"):
                backend.begin()
        finally:
            backend.conn = real_conn

        assert backend._lock.acquire(blocking=False)
        backend._lock.release()

    def test_begin_blocks_on_cross_process_writer_then_reports_retryable(
        self, temp_db: Path
    ) -> None:
        """KI-084: two independent SQLiteBackend instances on the same file
        stand in for two OS processes — self._lock is per-instance, so
        unlike every other test in this class, it does NOT serialize these
        two; only SQLite's own file locking does. This is what makes this
        the right vehicle to exercise cross-process contention in-process.

        Before the fix, begin() issued a plain (deferred) BEGIN. This
        connection's later read-then-write inside the transaction would
        take its snapshot lazily and could hit SQLITE_BUSY_SNAPSHOT on the
        write — a stale-snapshot-upgrade failure SQLite never routes
        through the busy handler, so it failed instantly, before
        busy_timeout got any chance to retry. With BEGIN IMMEDIATE, begin()
        itself claims the write lock, so a concurrent begin() is queued
        behind SQLite's ordinary busy handler and only fails after
        genuinely waiting out busy_timeout - proven here by asserting the
        elapsed time, not just the exception - with a message that now
        distinguishes lock contention from a genuine storage failure.
        """
        writer = SQLiteBackend(temp_db)
        contender = SQLiteBackend(temp_db)
        try:
            writer.put_principal(
                Principal(
                    id="alice@test.com",
                    kind="human",
                    auth_method="oidc",
                    created_at=datetime(2025, 1, 1, tzinfo=UTC),
                )
            )
            # Short busy_timeout on the contender only, so the test doesn't
            # have to wait out the real 5s default to prove the mechanism.
            contender.conn.execute("PRAGMA busy_timeout = 200")

            writer.begin()
            writer.put_entity(
                Entity(
                    id="entity-001",
                    namespace="test-ns",
                    concept="Person",
                    created_at=datetime(2025, 1, 1, tzinfo=UTC),
                    created_by="alice@test.com",
                )
            )
            # writer's transaction is left open, holding the write lock.

            t0 = time.monotonic()
            with pytest.raises(StorageError, match="lock contention.*safe to retry"):
                contender.begin()
            elapsed = time.monotonic() - t0

            # Failed only after genuinely waiting out busy_timeout, not
            # instantly (the pre-fix SQLITE_BUSY_SNAPSHOT failure mode).
            assert elapsed >= 0.15
        finally:
            writer.rollback()
            writer.close()
            contender.close()

    def test_begin_retries_and_succeeds_once_cross_process_writer_releases(
        self, temp_db: Path
    ) -> None:
        """KI-084: the positive counterpart to the test above — if the
        other process's write lock is released before busy_timeout
        expires, the busy handler's retry succeeds rather than eventually
        failing. Confirms BEGIN IMMEDIATE's contention is genuinely
        transient/retryable, not just distinguishable-but-still-fatal.

        Without the `elapsed` assertion below, this test has zero
        mutation-detecting power: with a plain deferred `BEGIN`,
        `contender.begin()` also returns immediately and also observes
        `entity-001` (its lazy read snapshot is only taken *after*
        `releaser.join()` has let the writer commit) — so the two
        assertions that used to be here alone passed identically whether
        or not `begin()` actually contended on anything. Asserting a
        blocking wait, mirroring the sibling test above, is what actually
        exercises BEGIN IMMEDIATE claiming the lock up front.
        """
        writer = SQLiteBackend(temp_db)
        contender = SQLiteBackend(temp_db)
        errors: list[BaseException] = []
        entered = threading.Event()
        try:
            writer.put_principal(
                Principal(
                    id="alice@test.com",
                    kind="human",
                    auth_method="oidc",
                    created_at=datetime(2025, 1, 1, tzinfo=UTC),
                )
            )

            # self._lock (a threading.RLock, KI-023) is owned by whichever
            # thread acquires it — begin() and the later commit() must run
            # on the same thread, so the whole hold-then-release sequence
            # is done here rather than split across threads.
            def hold_lock_then_release() -> None:
                try:
                    writer.begin()
                    writer.put_entity(
                        Entity(
                            id="entity-001",
                            namespace="test-ns",
                            concept="Person",
                            created_at=datetime(2025, 1, 1, tzinfo=UTC),
                            created_by="alice@test.com",
                        )
                    )
                    entered.set()
                    time.sleep(0.5)
                    writer.commit()
                except BaseException as e:  # noqa: BLE001 - captured for the assertion below
                    errors.append(e)

            releaser = threading.Thread(target=hold_lock_then_release)
            releaser.start()
            assert entered.wait(timeout=5)  # deterministic: writer's begin() has landed

            # contender keeps the (default, 5s) busy_timeout — plenty of
            # room for the writer's commit() above to land first; begin()
            # blocks here until the busy handler's retry succeeds.
            t0 = time.monotonic()
            contender.begin()
            elapsed = time.monotonic() - t0
            releaser.join(timeout=5)
            assert not releaser.is_alive()

            assert errors == []
            # Genuinely blocked on the writer's held lock, not a no-op
            # deferred BEGIN that never contended on anything. Threshold
            # kept well below the 0.5s sleep above to absorb scheduling
            # jitter between entered.wait() returning and t0 being taken.
            assert elapsed >= 0.3
            assert contender._in_transaction is True
            assert contender.get_entity("entity-001") is not None
        finally:
            if contender._in_transaction:
                contender.rollback()
            writer.close()
            contender.close()
