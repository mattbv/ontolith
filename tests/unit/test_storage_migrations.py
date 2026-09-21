"""Unit tests for on-disk format-version migrations (SPEC §15, ADR-0052).

Covers both backends' `migrations` module directly — `migrate_file()`'s
dry-run/real-run contract, `SQLiteBackend`/`DuckDBBackend` refusing a stale
or too-new file, and each registered migration's declared reversibility
(`_MIGRATIONS`, `up`/`down`) — plus `test_sqlite_backend.py`'s/
`test_duckdb_backend.py`'s own two migration-specific tests, which exercise
the same refusal + migrate_file() path end to end against a legacy fixture
built from raw DDL (KI-060/KI-078's historical shapes) rather than the
registry directly.
"""

import sqlite3
from pathlib import Path

import duckdb
import pytest

from ontolith.core.errors import SchemaError
from ontolith.store.duckdb import migrations as duckdb_migrations
from ontolith.store.duckdb.backend import DuckDBBackend
from ontolith.store.sqlite import migrations as sqlite_migrations
from ontolith.store.sqlite.backend import SQLiteBackend


class TestSQLiteMigrations:
    def test_fresh_database_is_stamped_at_current_version_with_no_steps(
        self, tmp_path: Path
    ) -> None:
        backend = SQLiteBackend(tmp_path / "fresh.db")
        try:
            row = backend.conn.execute("SELECT version FROM format_version").fetchone()
            assert row[0] == sqlite_migrations.CURRENT_FORMAT_VERSION
        finally:
            backend.close()

    def test_placeholder_zero_byte_file_is_treated_as_fresh_not_stale(self, tmp_path: Path) -> None:
        """A path that already exists as an empty file (e.g.
        `tempfile.NamedTemporaryFile(delete=False)`, as `test_sqlite_backend.py`'s
        own `temp_db` fixture uses) must connect cleanly, not be misread as a
        legacy version-1 file with no tables to introspect."""
        path = tmp_path / "placeholder.db"
        path.touch()
        assert path.stat().st_size == 0
        backend = SQLiteBackend(path)
        try:
            row = backend.conn.execute("SELECT version FROM format_version").fetchone()
            assert row[0] == sqlite_migrations.CURRENT_FORMAT_VERSION
        finally:
            backend.close()

    def _build_v1_file(self, path: Path) -> None:
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT)"
        )
        conn.execute("CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT)")
        conn.commit()
        conn.close()

    def test_stale_file_is_refused_not_silently_upgraded(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.db"
        self._build_v1_file(path)
        with pytest.raises(SchemaError, match="format_version 1"):
            SQLiteBackend(path)

    def test_dry_run_reports_pending_steps_without_mutating_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.db"
        self._build_v1_file(path)

        report = sqlite_migrations.migrate_file(path, dry_run=True)

        assert report.dry_run is True
        assert report.from_version == 1
        assert report.to_version == sqlite_migrations.CURRENT_FORMAT_VERSION
        assert [s.version for s in report.steps] == [2, 3]
        assert all(not s.applied for s in report.steps)
        assert all(s.reversible for s in report.steps)

        # Nothing on disk changed - not even the format_version table itself.
        conn = sqlite3.connect(str(path))
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            assert "format_version" not in tables
            columns = {r[1] for r in conn.execute("PRAGMA table_info(proposal)")}
            assert "reviewers" not in columns
        finally:
            conn.close()

        # A dry run must still raise SchemaError on open - it's a preview,
        # not a fix.
        with pytest.raises(SchemaError, match="format_version 1"):
            SQLiteBackend(path)

    def test_real_run_applies_every_pending_step_and_unblocks_connect(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.db"
        self._build_v1_file(path)

        report = sqlite_migrations.migrate_file(path)
        assert report.dry_run is False
        assert [s.applied for s in report.steps] == [True, True]

        backend = SQLiteBackend(path)
        try:
            row = backend.conn.execute("SELECT version FROM format_version").fetchone()
            assert row[0] == sqlite_migrations.CURRENT_FORMAT_VERSION
        finally:
            backend.close()

    def test_migrate_file_is_idempotent_on_an_already_current_file(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.db"
        self._build_v1_file(path)
        sqlite_migrations.migrate_file(path)

        second = sqlite_migrations.migrate_file(path)
        assert second.up_to_date
        assert second.from_version == sqlite_migrations.CURRENT_FORMAT_VERSION
        assert second.steps == ()

    def test_migrate_file_on_nonexistent_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SchemaError, match="No database file"):
            sqlite_migrations.migrate_file(tmp_path / "does_not_exist.db")

    def test_file_newer_than_supported_is_refused_on_connect_and_migrate(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "from_future.db"
        backend = SQLiteBackend(path)
        backend.conn.execute("UPDATE format_version SET version = 99")
        backend.close()

        with pytest.raises(SchemaError, match="newer than this build"):
            SQLiteBackend(path)
        with pytest.raises(SchemaError, match="newer than this build"):
            sqlite_migrations.migrate_file(path)

    def test_already_v3_shaped_legacy_file_with_no_tracking_row_is_recognized_as_current(
        self, tmp_path: Path
    ) -> None:
        """A file built by the pre-framework ad hoc fixups already has every
        v3 column but predates `format_version` entirely - inference must
        land on 3 (not the pessimistic default of 1), so connecting neither
        refuses nor re-runs any migration; it just stamps the row."""
        path = tmp_path / "already_v3.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT, issued_by TEXT, revoked_by TEXT)"
        )
        conn.execute(
            "CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT, "
            "reviewers TEXT NOT NULL DEFAULT '[]')"
        )
        conn.commit()
        conn.close()

        report = sqlite_migrations.migrate_file(path)
        assert report.up_to_date
        assert report.from_version == sqlite_migrations.CURRENT_FORMAT_VERSION

        backend = SQLiteBackend(path)
        try:
            row = backend.conn.execute("SELECT version FROM format_version").fetchone()
            assert row[0] == sqlite_migrations.CURRENT_FORMAT_VERSION
        finally:
            backend.close()

    def test_format_version_table_present_but_empty_falls_back_to_inference(
        self, tmp_path: Path
    ) -> None:
        """Defensive branch: the tracking table exists (so this project's
        own code, not a stray external `CREATE TABLE`, made it) but has no
        row yet - `read_current_version` must still fall back to column
        inference rather than crash on `fetchone() is None`."""
        path = tmp_path / "table_no_row.db"
        self._build_v1_file(path)
        conn = sqlite3.connect(str(path))
        sqlite_migrations._ensure_format_version_table(conn.cursor())
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(path))
        try:
            assert sqlite_migrations.read_current_version(conn.cursor()) == 1
        finally:
            conn.close()

    def test_each_migration_reversal_restores_the_prior_shape(self, tmp_path: Path) -> None:
        """White-box test of `_MIGRATIONS`' declared `down()` — there's no
        public downgrade entry point (SPEC §15 requires a migration declare
        reversibility, not that this project ship a CLI for it yet), so this
        exercises the registry directly, same as its own dense/contiguous
        assert already does at import time."""
        path = tmp_path / "fresh.db"
        backend = SQLiteBackend(path)
        cursor = backend.conn.cursor()
        try:
            for migration in reversed(sqlite_migrations._MIGRATIONS):
                assert migration.reversible
                assert migration.down is not None
                migration.down(cursor)
            columns = {r[1] for r in cursor.execute("PRAGMA table_info(principal_credential)")}
            assert "issued_by" not in columns
            assert "revoked_by" not in columns
            proposal_columns = {r[1] for r in cursor.execute("PRAGMA table_info(proposal)")}
            assert "reviewers" not in proposal_columns
        finally:
            backend.close()


class TestDuckDBMigrations:
    def test_fresh_database_is_stamped_at_current_version_with_no_steps(
        self, tmp_path: Path
    ) -> None:
        backend = DuckDBBackend(tmp_path / "fresh.duckdb")
        try:
            row = backend.conn.execute("SELECT version FROM format_version").fetchone()
            assert row[0] == duckdb_migrations.CURRENT_FORMAT_VERSION
        finally:
            backend.close()

    def _build_v1_file(self, path: Path) -> None:
        conn = duckdb.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT)"
        )
        conn.execute("CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT)")
        conn.close()

    def test_stale_file_is_refused_not_silently_upgraded(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.duckdb"
        self._build_v1_file(path)
        with pytest.raises(SchemaError, match="format_version 1"):
            DuckDBBackend(path)

    def test_dry_run_reports_pending_steps_without_mutating_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.duckdb"
        self._build_v1_file(path)

        report = duckdb_migrations.migrate_file(path, dry_run=True)

        assert report.dry_run is True
        assert report.from_version == 1
        assert [s.version for s in report.steps] == [2, 3]
        assert all(not s.applied for s in report.steps)

        conn = duckdb.connect(str(path))
        try:
            tables = {
                r[0]
                for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()
            }
            assert "format_version" not in tables
        finally:
            conn.close()

        with pytest.raises(SchemaError, match="format_version 1"):
            DuckDBBackend(path)

    def test_real_run_applies_every_pending_step_and_unblocks_connect(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.duckdb"
        self._build_v1_file(path)

        report = duckdb_migrations.migrate_file(path)
        assert [s.applied for s in report.steps] == [True, True]

        backend = DuckDBBackend(path)
        try:
            row = backend.conn.execute("SELECT version FROM format_version").fetchone()
            assert row[0] == duckdb_migrations.CURRENT_FORMAT_VERSION
        finally:
            backend.close()

    def test_migrate_file_is_idempotent_on_an_already_current_file(self, tmp_path: Path) -> None:
        path = tmp_path / "legacy.duckdb"
        self._build_v1_file(path)
        duckdb_migrations.migrate_file(path)

        second = duckdb_migrations.migrate_file(path)
        assert second.up_to_date
        assert second.steps == ()

    def test_migrate_file_on_nonexistent_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SchemaError, match="No database file"):
            duckdb_migrations.migrate_file(tmp_path / "does_not_exist.duckdb")

    def test_file_newer_than_supported_is_refused_on_connect_and_migrate(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "from_future.duckdb"
        backend = DuckDBBackend(path)
        backend.conn.execute("UPDATE format_version SET version = 99")
        backend.close()

        with pytest.raises(SchemaError, match="newer than this build"):
            DuckDBBackend(path)
        with pytest.raises(SchemaError, match="newer than this build"):
            duckdb_migrations.migrate_file(path)

    def test_already_v3_shaped_legacy_file_with_no_tracking_row_is_recognized_as_current(
        self, tmp_path: Path
    ) -> None:
        """See the SQLite module's identical test - a file built by the
        pre-framework ad hoc fixups already has every v3 column but
        predates `format_version` entirely; inference must land on 3."""
        path = tmp_path / "already_v3.duckdb"
        conn = duckdb.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT, issued_by TEXT, revoked_by TEXT)"
        )
        conn.execute(
            "CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT, "
            "reviewers TEXT DEFAULT '[]')"
        )
        conn.close()

        report = duckdb_migrations.migrate_file(path)
        assert report.up_to_date
        assert report.from_version == duckdb_migrations.CURRENT_FORMAT_VERSION

        backend = DuckDBBackend(path)
        try:
            row = backend.conn.execute("SELECT version FROM format_version").fetchone()
            assert row[0] == duckdb_migrations.CURRENT_FORMAT_VERSION
        finally:
            backend.close()

    def test_format_version_table_present_but_empty_falls_back_to_inference(
        self, tmp_path: Path
    ) -> None:
        """See the SQLite module's identical test - the tracking table
        exists but has no row yet; `read_current_version` must fall back to
        column inference rather than crash on `fetchone() is None`."""
        path = tmp_path / "table_no_row.duckdb"
        self._build_v1_file(path)
        conn = duckdb.connect(str(path))
        try:
            duckdb_migrations._ensure_format_version_table(conn)
            assert duckdb_migrations.read_current_version(conn) == 1
        finally:
            conn.close()

    def test_each_migration_reversal_restores_the_prior_shape(self, tmp_path: Path) -> None:
        path = tmp_path / "fresh.duckdb"
        backend = DuckDBBackend(path)
        try:
            for migration in reversed(duckdb_migrations._MIGRATIONS):
                assert migration.reversible
                assert migration.down is not None
                migration.down(backend.conn)
            columns = {
                r[0]
                for r in backend.conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'principal_credential'"
                ).fetchall()
            }
            assert "issued_by" not in columns
            assert "revoked_by" not in columns
            proposal_columns = {
                r[0]
                for r in backend.conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'proposal'"
                ).fetchall()
            }
            assert "reviewers" not in proposal_columns
        finally:
            backend.close()
