"""Conformance vector: Accountable owner rule for AI principals.

SPEC §8 — AI principals MUST declare an accountable owner (human or team).
This invariant is enforced at three layers:
  1. Pydantic model (owner must be non-null for ai-kind)
  2. Application layer (Ontology.create_principal: owner must resolve to an
     existing human/service principal, not just be a non-null string)
  3. Storage layer (DB CHECK + FOREIGN KEY constraints — ADR-0011)
"""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conformance.conftest import KbFactory
from ontolith import Ontology, SequentialIdProvider
from ontolith.core import FixedClock
from ontolith.core.errors import StorageError, ValidationError
from ontolith.identity import Principal

T0 = "2025-01-01T00:00:00+00:00"


def _kb(make_kb: KbFactory) -> Ontology:
    return make_kb(FixedClock(T0), SequentialIdProvider("cv"))


def test_ai_principal_with_owner_is_accepted(make_kb: KbFactory) -> None:
    """AI principal with a declared, resolvable owner is accepted (SPEC §8)."""
    kb = _kb(make_kb)
    kb.create_principal("alice@example.com", kind="human")
    principal = kb.create_principal(
        "scout-agent",
        kind="ai",
        auth_method="workload",
        owner="alice@example.com",
    )
    assert principal.id == "scout-agent"
    assert principal.kind == "ai"
    assert principal.owner == "alice@example.com"
    kb.close()


def test_ai_principal_without_owner_raises_at_application_layer(make_kb: KbFactory) -> None:
    """AI principal without owner is rejected before reaching the DB (SPEC §8)."""
    kb = _kb(make_kb)
    with pytest.raises((ValueError, StorageError)):
        kb.create_principal("bot-no-owner", kind="ai", auth_method="workload")
    kb.close()


def test_ai_principal_owner_survives_roundtrip(make_kb: KbFactory) -> None:
    """Owner field is persisted and retrieved correctly."""
    kb = _kb(make_kb)
    kb.create_principal("alice@example.com", kind="human")
    kb.create_principal(
        "scout-agent",
        kind="ai",
        auth_method="workload",
        owner="alice@example.com",
    )
    retrieved = kb.get_principal("scout-agent")
    assert retrieved is not None
    assert retrieved.owner == "alice@example.com"
    kb.close()


def test_ai_principal_with_unresolvable_owner_rejected(make_kb: KbFactory) -> None:
    """Owner must name an EXISTING principal, not just be a non-null string —
    otherwise the accountable-owner guarantee is fiction (SPEC §8.1)."""
    kb = _kb(make_kb)
    with pytest.raises(ValidationError, match="owner not found"):
        kb.create_principal(
            "scout-agent",
            kind="ai",
            auth_method="workload",
            owner="ghost@example.com",
        )
    kb.close()


def test_ai_principal_owner_cannot_be_another_ai(make_kb: KbFactory) -> None:
    """The accountable owner must be human/service — an AI cannot be
    accountable for another AI (SPEC §8.1)."""
    kb = _kb(make_kb)
    kb.create_principal("alice@example.com", kind="human")
    kb.create_principal("scout-agent", kind="ai", auth_method="workload", owner="alice@example.com")

    with pytest.raises(ValidationError, match="must be human or service"):
        kb.create_principal(
            "downstream-bot",
            kind="ai",
            auth_method="workload",
            owner="scout-agent",
        )
    kb.close()


def test_human_principal_owner_is_optional(make_kb: KbFactory) -> None:
    """Human principals do not require an owner (SPEC §8)."""
    kb = _kb(make_kb)
    principal = kb.create_principal("alice@example.com", kind="human")
    assert principal.owner is None
    kb.close()


def test_service_principal_owner_is_optional(make_kb: KbFactory) -> None:
    """Service principals do not require an owner (SPEC §8)."""
    kb = _kb(make_kb)
    principal = kb.create_principal("etl-service", kind="service", auth_method="apikey")
    assert principal.owner is None
    kb.close()


def test_ai_owner_invariant_enforced_at_db_layer() -> None:
    """DB CHECK constraint rejects AI rows without owner (ADR-0011 defense-in-depth).

    SQLite-specific: asserts on sqlite3.IntegrityError directly. Not
    parametrized via make_kb — see test_ai_owner_invariant_enforced_at_duckdb_layer
    for the DuckDB equivalent.
    """
    import sqlite3

    from ontolith.store.sqlite import SQLiteBackend

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = Path(f.name)
    f.close()
    backend = SQLiteBackend(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            backend.conn.execute(
                """
                INSERT INTO principal (id, kind, auth_method, trust_level, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("bot-bypass", "ai", "workload", 0, "2025-01-01T00:00:00Z", "{}"),
            )
    finally:
        backend.close()
        path.unlink()


def test_ai_owner_fk_enforced_at_db_layer() -> None:
    """DB FOREIGN KEY constraint rejects an owner that isn't an existing
    principal row, even if Ontology.create_principal is bypassed
    (ADR-0011 defense-in-depth).

    SQLite-specific: asserts on sqlite3.IntegrityError directly. Not
    parametrized via make_kb — see test_ai_owner_fk_enforced_at_duckdb_layer
    for the DuckDB equivalent.
    """
    import sqlite3

    from ontolith.store.sqlite import SQLiteBackend

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = Path(f.name)
    f.close()
    backend = SQLiteBackend(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            backend.conn.execute(
                """
                INSERT INTO principal (id, kind, owner, auth_method, trust_level, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "bot-bypass",
                    "ai",
                    "ghost@example.com",
                    "workload",
                    0,
                    "2025-01-01T00:00:00Z",
                    "{}",
                ),
            )
    finally:
        backend.close()
        path.unlink()


def test_ai_owner_invariant_enforced_at_duckdb_layer(tmp_path: Path) -> None:
    """DB CHECK constraint rejects AI rows without owner (ADR-0011, DuckDB)."""
    import duckdb

    from ontolith.store.duckdb import DuckDBBackend

    backend = DuckDBBackend(tmp_path / "test.db")
    try:
        with pytest.raises(duckdb.Error):
            backend.conn.execute(
                """
                INSERT INTO principal (id, kind, auth_method, trust_level, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ["bot-bypass", "ai", "workload", 0, "2025-01-01T00:00:00Z", "{}"],
            )
    finally:
        backend.close()


def test_ai_owner_fk_enforced_at_duckdb_layer(tmp_path: Path) -> None:
    """DB FOREIGN KEY constraint rejects an owner that isn't an existing
    principal row, even if Ontology.create_principal is bypassed (ADR-0011, DuckDB).
    """
    import duckdb

    from ontolith.store.duckdb import DuckDBBackend

    backend = DuckDBBackend(tmp_path / "test.db")
    try:
        with pytest.raises(duckdb.Error):
            backend.conn.execute(
                """
                INSERT INTO principal (id, kind, owner, auth_method, trust_level, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    "bot-bypass",
                    "ai",
                    "ghost@example.com",
                    "workload",
                    0,
                    "2025-01-01T00:00:00Z",
                    "{}",
                ],
            )
    finally:
        backend.close()


def test_pydantic_model_rejects_ai_without_owner() -> None:
    """Principal Pydantic model enforces owner requirement for AI kind."""
    with pytest.raises(ValueError):
        Principal(
            id="bot",
            kind="ai",
            auth_method="workload",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
