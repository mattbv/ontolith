"""Conformance vector: audit table immutability (SPEC §17, KI-066).

SPEC §17: "the audit trail MUST NOT be mutable." Prior to this KI, that was
enforced only by StorageBackend's port surface exposing no update/delete
method for `assertion_event`/`proposal_event` — a convention any code
holding the raw connection could bypass. SQLiteBackend now backs this with
three triggers per table: `BEFORE UPDATE`, `BEFORE DELETE`, and `BEFORE
INSERT ... WHEN EXISTS(...)` (the last closes `INSERT OR REPLACE`, which
performs an implicit conflict-row delete that a plain `BEFORE DELETE`
trigger only catches when `PRAGMA recursive_triggers` is ON — found in
review round 1). `recursive_triggers` is also set ON in `__init__` as
defense in depth, but is NOT load-bearing for the REPLACE case: the
`BEFORE INSERT` trigger persists in the schema itself and blocks REPLACE
regardless of pragma state, unlike the pragma, which is per-*connection*
and doesn't follow a second raw connection to the same file (found in
review round 2 — see `TestSecondConnectionCannotBypassEitherTrigger`).

DuckDB has no `CREATE TRIGGER` support at all (verified against 1.5.4) —
DuckDBBackend's own docstring documents this as a known, currently
unfixable asymmetry. The DuckDB tests below pin the *current* state (these
operations still succeed) so a regression silently dropping the SQLite
triggers is caught by the SQLite tests flipping; they do NOT by themselves
prove anything about a future DuckDB release, since DuckDBBackend's own
schema-creation code — not the `duckdb` package version — is what would
need to change for that to matter (a bare dependency bump alone would
leave these green with nothing to catch).

Not parametrized via `make_kb` — mirrors test_accountable_owner.py's own
raw-connection/backend-internal DB-layer tests (see that file and
conftest.py's module docstring for why).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ontolith import Ontology, SequentialIdProvider
from ontolith.core import FixedClock
from ontolith.store.base import StorageBackend

T0 = "2025-01-01T00:00:00+00:00"


def _seed_assertion_event(backend: StorageBackend) -> str:
    """Retracts an assertion (recording a real assertion_event row via
    Ontology._record_assertion_event) and returns the event's id."""
    kb = Ontology(backend, clock=FixedClock(T0), id_provider=SequentialIdProvider("cv"))
    kb.create_principal("alice@example.com", kind="human", default_capability="write")
    entity = kb.create_entity("Person", author="alice@example.com")
    assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "alice@example.com")
    kb.retract(assertion.id, "alice@example.com")
    [event] = kb.backend.get_assertion_events(assertion.id)
    return event.id


def _seed_proposal_event(backend: StorageBackend) -> str:
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

    def test_assertion_event_replace_rejected(self, tmp_path: Path) -> None:
        """`INSERT OR REPLACE` with an existing id performs a conflict-row
        DELETE before the new row is inserted - without `recursive_triggers
        = ON`, that DELETE doesn't fire trg_assertion_event_no_delete
        (found in review: a real bypass distinct from a plain DELETE,
        since REPLACE can also rewrite every other column including
        `actor` - attribution laundering, not just row loss - this test
        only needs to prove the trigger fires, not exercise that angle)."""
        import sqlite3

        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(tmp_path / "test.db")
        try:
            event_id = _seed_assertion_event(backend)
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                backend.conn.execute(
                    """
                    INSERT OR REPLACE INTO assertion_event
                        (id, assertion_id, actor, action, at)
                    SELECT id, assertion_id, actor, 'flagged', at
                    FROM assertion_event WHERE id = ?
                    """,
                    (event_id,),
                )
        finally:
            backend.close()

    def test_proposal_event_replace_rejected(self, tmp_path: Path) -> None:
        """Same REPLACE-bypass shape as
        test_assertion_event_replace_rejected, for proposal_event."""
        import sqlite3

        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(tmp_path / "test.db")
        try:
            event_id = _seed_proposal_event(backend)
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                backend.conn.execute(
                    """
                    INSERT OR REPLACE INTO proposal_event
                        (id, proposal_id, actor, type, detail, at)
                    SELECT id, proposal_id, actor, type, 'TAMPERED', at
                    FROM proposal_event WHERE id = ?
                    """,
                    (event_id,),
                )
        finally:
            backend.close()


class TestSecondConnectionCannotBypassEitherTrigger:
    """Round-2 review finding: `PRAGMA recursive_triggers` is per-
    *connection* state, not persisted in the database file - a second raw
    connection to the same file (e.g. via `backend.path`, a public
    attribute one hop past `ReadOnlyView._backend`, KI-066's own named
    threat model) opens with the pragma OFF regardless of what
    `SQLiteBackend.__init__` set on its own connection, reviving the
    REPLACE bypass with no privilege escalation needed. Closed durably by
    `trg_*_no_replace` (a `BEFORE INSERT ... WHEN EXISTS` trigger, which
    persists in the schema and needs no pragma at all) rather than relying
    on the pragma alone - these tests open a second connection and
    deliberately do NOT set `recursive_triggers`, to prove the schema-level
    trigger is what's actually doing the work."""

    def test_second_connection_replace_still_rejected(self, tmp_path: Path) -> None:
        import sqlite3

        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(tmp_path / "test.db")
        try:
            event_id = _seed_assertion_event(backend)
            second_conn = sqlite3.connect(backend.path)
            try:
                assert second_conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    second_conn.execute(
                        """
                        INSERT OR REPLACE INTO assertion_event
                            (id, assertion_id, actor, action, at)
                        SELECT id, assertion_id, actor, 'flagged', at
                        FROM assertion_event WHERE id = ?
                        """,
                        (event_id,),
                    )
            finally:
                second_conn.close()
        finally:
            backend.close()

    def test_second_connection_update_and_delete_still_rejected(self, tmp_path: Path) -> None:
        import sqlite3

        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(tmp_path / "test.db")
        try:
            event_id = _seed_proposal_event(backend)
            second_conn = sqlite3.connect(backend.path)
            try:
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    second_conn.execute(
                        "UPDATE proposal_event SET detail = 'edited' WHERE id = ?", (event_id,)
                    )
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    second_conn.execute("DELETE FROM proposal_event WHERE id = ?", (event_id,))
            finally:
                second_conn.close()
        finally:
            backend.close()


class TestDuckDBAuditTablesAreCurrentlyMutable:
    """Pins the documented KI-066 asymmetry: DuckDB has no CREATE TRIGGER
    support, so these operations currently still succeed. This is NOT a
    forward-looking canary for a DuckDB version bump alone - DuckDBBackend
    installs no triggers regardless of the duckdb package version, so
    these stay green until DuckDBBackend's own schema-creation code
    changes. If it ever does, update these tests (and DuckDBBackend's
    docstring) together."""

    def test_assertion_event_update_currently_succeeds(self, tmp_path: Path) -> None:
        from ontolith.store.duckdb import DuckDBBackend

        backend = DuckDBBackend(tmp_path / "test.db")
        try:
            event_id = _seed_assertion_event(backend)
            backend.conn.execute(
                "UPDATE assertion_event SET action = 'flagged' WHERE id = ?", [event_id]
            )
            row = backend.conn.execute(
                "SELECT action FROM assertion_event WHERE id = ?", [event_id]
            ).fetchone()
            assert row is not None
            assert row[0] == "flagged"
        finally:
            backend.close()

    def test_proposal_event_delete_currently_succeeds(self, tmp_path: Path) -> None:
        from ontolith.store.duckdb import DuckDBBackend

        backend = DuckDBBackend(tmp_path / "test.db")
        try:
            event_id = _seed_proposal_event(backend)
            backend.conn.execute("DELETE FROM proposal_event WHERE id = ?", [event_id])
            row = backend.conn.execute(
                "SELECT 1 FROM proposal_event WHERE id = ?", [event_id]
            ).fetchone()
            assert row is None
        finally:
            backend.close()
