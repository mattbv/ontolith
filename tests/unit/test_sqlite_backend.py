"""Unit tests for SQLite storage backend."""

import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith.core import Assertion, Entity, FixedClock
from ontolith.core.errors import StorageError
from ontolith.govern.contradiction import Contradiction
from ontolith.govern.proposal import Proposal, ProposalEvent
from ontolith.identity import Principal, PrincipalCredential
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

        second = SQLiteBackend(temp_db)
        [after] = second.list_namespaces()

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
        backend.revoke_credential("cred-1", datetime(2025, 1, 2, tzinfo=UTC))

        assert backend.get_principal_by_token_hash("deadbeef" * 8) is None

    def test_revoke_nonexistent_credential_raises(self, backend: SQLiteBackend) -> None:
        with pytest.raises(StorageError, match="Credential not found"):
            backend.revoke_credential("nonexistent", datetime(2025, 1, 1, tzinfo=UTC))

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

        backend.revoke_credential("cred-1", datetime(2025, 1, 3, tzinfo=UTC))
        assert backend.get_principal_by_token_hash("a" * 64) is None
        assert backend.get_principal_by_token_hash("b" * 64) is not None

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
