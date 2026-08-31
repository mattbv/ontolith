"""Conformance vector: audit table immutability (SPEC §17, KI-066).

SPEC §17: "the audit trail MUST NOT be mutable." Prior to this KI, that was
enforced only by StorageBackend's port surface exposing no update/delete
method for `assertion_event`/`proposal_event` — a convention any code
holding the raw connection could bypass. SQLiteBackend now backs this with
`BEFORE UPDATE`/`BEFORE DELETE` triggers on both tables (store-level
guarantee). DuckDB has no `CREATE TRIGGER` support at all (verified against
1.5.4) — DuckDBBackend's own docstring documents this as a known, currently
unfixable asymmetry, and the tests below pin the DuckDB side as *currently
still mutable* so a change in either direction (DuckDB gaining triggers, or
someone silently dropping the SQLite ones) is caught rather than silently
drifting.

Not parametrized via `make_kb` — mirrors test_accountable_owner.py's own
raw-connection/backend-internal DB-layer tests (see that file and
conftest.py's module docstring for why).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ontolith import Ontology, SequentialIdProvider
from ontolith.core import FixedClock

T0 = "2025-01-01T00:00:00+00:00"


def _seed_assertion_event(backend) -> str:  # type: ignore[no-untyped-def]
    """Retracts an assertion (recording a real assertion_event row via
    Ontology._record_assertion_event) and returns the event's id."""
    kb = Ontology(backend, clock=FixedClock(T0), id_provider=SequentialIdProvider("cv"))
    kb.create_principal("alice@example.com", kind="human", default_capability="write")
    entity = kb.create_entity("Person", author="alice@example.com")
    assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "alice@example.com")
    kb.retract(assertion.id, "alice@example.com")
    [event] = kb.backend.get_assertion_events(assertion.id)
    return event.id


def _seed_proposal_event(backend) -> str:  # type: ignore[no-untyped-def]
    """Requests changes on a proposal (recording a real proposal_event row)
    and returns the event's id."""
    kb = Ontology(backend, clock=FixedClock(T0), id_provider=SequentialIdProvider("cv"))
    kb.create_principal("alice@example.com", kind="human", default_capability="propose")
    kb.create_principal("carol@example.com", kind="human", default_capability="review")
    entity = kb.create_entity("Person", author="carol@example.com")
    proposal, _decision = kb.propose(entity.id, "Person.name", "Ada", "Text", "alice@example.com")
    kb.request_changes(proposal.id, "carol@example.com", reason="needs a source")
    [event] = kb.backend.get_proposal_events(proposal.id)
    return event.id


class TestSQLiteAuditTablesAreImmutable:
    """Store-level enforcement: BEFORE UPDATE/DELETE triggers (KI-066)."""

    def test_assertion_event_update_rejected(self, tmp_path: Path) -> None:
        import sqlite3

        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(tmp_path / "test.db")
        try:
            event_id = _seed_assertion_event(backend)
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                backend.conn.execute(
                    "UPDATE assertion_event SET action = 'flagged' WHERE id = ?", (event_id,)
                )
        finally:
            backend.close()

    def test_assertion_event_delete_rejected(self, tmp_path: Path) -> None:
        import sqlite3

        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(tmp_path / "test.db")
        try:
            event_id = _seed_assertion_event(backend)
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                backend.conn.execute("DELETE FROM assertion_event WHERE id = ?", (event_id,))
        finally:
            backend.close()

    def test_proposal_event_update_rejected(self, tmp_path: Path) -> None:
        import sqlite3

        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(tmp_path / "test.db")
        try:
            event_id = _seed_proposal_event(backend)
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                backend.conn.execute(
                    "UPDATE proposal_event SET detail = 'edited' WHERE id = ?", (event_id,)
                )
        finally:
            backend.close()

    def test_proposal_event_delete_rejected(self, tmp_path: Path) -> None:
        import sqlite3

        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(tmp_path / "test.db")
        try:
            event_id = _seed_proposal_event(backend)
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                backend.conn.execute("DELETE FROM proposal_event WHERE id = ?", (event_id,))
        finally:
            backend.close()


class TestDuckDBAuditTablesAreCurrentlyMutable:
    """Pins the documented KI-066 asymmetry: DuckDB has no CREATE TRIGGER
    support, so these operations currently still succeed. If DuckDB ever
    adds trigger support and this is revisited, these tests should start
    failing loudly rather than silently staying green for the wrong
    reason - update them (and DuckDBBackend's docstring) together."""

    def test_assertion_event_update_currently_succeeds(self, tmp_path: Path) -> None:
        from ontolith.store.duckdb import DuckDBBackend

        backend = DuckDBBackend(tmp_path / "test.db")
        try:
            event_id = _seed_assertion_event(backend)
            backend.conn.execute(
                "UPDATE assertion_event SET action = 'flagged' WHERE id = ?", [event_id]
            )
        finally:
            backend.close()

    def test_proposal_event_delete_currently_succeeds(self, tmp_path: Path) -> None:
        from ontolith.store.duckdb import DuckDBBackend

        backend = DuckDBBackend(tmp_path / "test.db")
        try:
            event_id = _seed_proposal_event(backend)
            backend.conn.execute("DELETE FROM proposal_event WHERE id = ?", [event_id])
        finally:
            backend.close()
