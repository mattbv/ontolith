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
    """Store-level enforcement via the backend's own connection: BEFORE
    UPDATE / BEFORE DELETE / BEFORE INSERT (REPLACE-guard) triggers
    (KI-066)."""

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
        `actor` when the replacement is an existing principal id -
        attribution laundering, not just row loss).

        Matches specifically on "REPLACE is not permitted", not just
        "append-only": on this connection `recursive_triggers` is ON, so
        without trg_assertion_event_no_replace the *DELETE* trigger would
        catch the conflict-row removal instead and produce a
        DELETE-flavored message that also contains "append-only" - a
        broader match would pass even with trg_assertion_event_no_replace
        deleted, silently un-pinning the round-2 fix (found in review
        round 3: this exact gap existed on the proposal_event sibling
        test below until this round tightened both)."""
        import sqlite3

        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(tmp_path / "test.db")
        try:
            event_id = _seed_assertion_event(backend)
            with pytest.raises(sqlite3.IntegrityError, match="REPLACE is not permitted"):
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
        """Same REPLACE-bypass shape and same "REPLACE is not permitted"
        match precision as test_assertion_event_replace_rejected, for
        proposal_event."""
        import sqlite3

        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(tmp_path / "test.db")
        try:
            event_id = _seed_proposal_event(backend)
            with pytest.raises(sqlite3.IntegrityError, match="REPLACE is not permitted"):
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

    def test_assertion_event_unqualified_delete_rejected(self, tmp_path: Path) -> None:
        """A bare `DELETE FROM assertion_event` (no WHERE) is the most
        direct "wipe the log" statement, and the one SQLite's truncate
        optimization would otherwise fast-path - confirms the optimization
        is correctly disabled when delete triggers exist (found worth
        pinning in review round 3)."""
        import sqlite3

        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(tmp_path / "test.db")
        try:
            _seed_assertion_event(backend)
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                backend.conn.execute("DELETE FROM assertion_event")
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
    trigger is what's actually doing the work.

    Both tables get all three operations (UPDATE/DELETE/REPLACE) from the
    second connection - round 3 review found the first version of this
    class only exercised REPLACE for assertion_event and only
    UPDATE/DELETE for proposal_event, leaving each table's REPLACE-from-a-
    second-connection path only half covered."""

    def test_assertion_event_second_connection_rejected(self, tmp_path: Path) -> None:
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
                        "UPDATE assertion_event SET action = 'flagged' WHERE id = ?", (event_id,)
                    )
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    second_conn.execute("DELETE FROM assertion_event WHERE id = ?", (event_id,))
                with pytest.raises(sqlite3.IntegrityError, match="REPLACE is not permitted"):
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

    def test_proposal_event_second_connection_rejected(self, tmp_path: Path) -> None:
        import sqlite3

        from ontolith.store.sqlite import SQLiteBackend

        backend = SQLiteBackend(tmp_path / "test.db")
        try:
            event_id = _seed_proposal_event(backend)
            second_conn = sqlite3.connect(backend.path)
            try:
                assert second_conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    second_conn.execute(
                        "UPDATE proposal_event SET detail = 'edited' WHERE id = ?", (event_id,)
                    )
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    second_conn.execute("DELETE FROM proposal_event WHERE id = ?", (event_id,))
                with pytest.raises(sqlite3.IntegrityError, match="REPLACE is not permitted"):
                    second_conn.execute(
                        """
                        INSERT OR REPLACE INTO proposal_event
                            (id, proposal_id, actor, type, detail, at)
                        SELECT id, proposal_id, actor, type, 'TAMPERED', at
                        FROM proposal_event WHERE id = ?
                        """,
                        (event_id,),
                    )
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

    def test_assertion_event_replace_currently_succeeds(self, tmp_path: Path) -> None:
        """DuckDB supports `INSERT OR REPLACE` natively (unlike SQLite,
        no pragma involved) - included for symmetry with the SQLite side's
        REPLACE coverage, not because DuckDB has any REPLACE-specific
        mechanism to pin beyond "no triggers exist at all"."""
        from ontolith.store.duckdb import DuckDBBackend

        backend = DuckDBBackend(tmp_path / "test.db")
        try:
            event_id = _seed_assertion_event(backend)
            backend.conn.execute(
                """
                INSERT OR REPLACE INTO assertion_event
                    (id, assertion_id, actor, action, "at")
                SELECT id, assertion_id, actor, 'flagged', "at"
                FROM assertion_event WHERE id = ?
                """,
                [event_id],
            )
            row = backend.conn.execute(
                "SELECT action FROM assertion_event WHERE id = ?", [event_id]
            ).fetchone()
            assert row is not None
            assert row[0] == "flagged"
        finally:
            backend.close()
