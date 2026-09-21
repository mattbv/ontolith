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
import time
from pathlib import Path

import duckdb
import pytest

from ontolith.core.errors import SchemaError, StorageError
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

    def test_require_current_format_stamps_when_table_exists_but_row_is_absent(
        self, tmp_path: Path
    ) -> None:
        """Same defensive branch as above (table exists, no row yet), but
        for an already-v3-shaped file — the specific case that reaches
        `require_current_format`'s own table-exists-but-row-check (a v1
        file never gets there at all, since it's refused earlier on the
        version-too-low check, before this branch is ever reached).
        `require_current_format` must fall through to actually stamping
        the row rather than crashing or wrongly trusting an empty table as
        "already confirmed"."""
        path = tmp_path / "already_v3_empty_table.db"
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
        sqlite_migrations._ensure_format_version_table(conn.cursor())
        conn.commit()
        conn.close()

        backend = SQLiteBackend(path)
        try:
            row = backend.conn.execute("SELECT version FROM format_version").fetchone()
            assert row[0] == sqlite_migrations.CURRENT_FORMAT_VERSION
        finally:
            backend.close()

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

    def test_require_current_format_stamps_when_table_exists_but_row_is_absent(
        self, tmp_path: Path
    ) -> None:
        """See the SQLite module's identical test - an already-v3-shaped
        file (not v1 - that's refused before this branch is ever reached)
        with an empty `format_version` table, exercised through
        `require_current_format`."""
        path = tmp_path / "already_v3_empty_table.duckdb"
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
        duckdb_migrations._ensure_format_version_table(conn)
        conn.close()

        backend = DuckDBBackend(path)
        try:
            row = backend.conn.execute("SELECT version FROM format_version").fetchone()
            assert row[0] == duckdb_migrations.CURRENT_FORMAT_VERSION
        finally:
            backend.close()

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


class TestRoundOneReviewFindings:
    """Regression tests for round-1 review of ADR-0052 — each of these
    reproduced a real bug before its corresponding fix landed; see
    ADR-0052's own Update section for the full record."""

    def test_h1_sqlite_file_missing_principal_credential_entirely_is_not_misread_as_current(
        self, tmp_path: Path
    ) -> None:
        """H1: a real, older shape from before `principal_credential`
        existed at all (added after `proposal`, per git history) —
        `proposal` exists, missing `reviewers`; `principal_credential` is
        absent entirely, not just missing columns. This used to be misread
        as "empty database", stamped `format_version=3`, and left
        `proposal.reviewers` permanently missing. Each table must now be
        checked independently."""
        path = tmp_path / "pre_credential.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT)")
        conn.commit()
        conn.close()

        report = sqlite_migrations.migrate_file(path, dry_run=True)
        assert report.from_version == 2
        assert [s.version for s in report.steps] == [3]

        with pytest.raises(SchemaError, match="format_version 2"):
            SQLiteBackend(path)

        sqlite_migrations.migrate_file(path)
        backend = SQLiteBackend(path)
        try:
            columns = {r[1] for r in backend.conn.execute("PRAGMA table_info(proposal)")}
            assert "reviewers" in columns
            # principal_credential was missing entirely - _create_schema()
            # creates it fresh, at the current shape, no migration needed.
            cred_columns = {
                r[1] for r in backend.conn.execute("PRAGMA table_info(principal_credential)")
            }
            assert {"issued_by", "revoked_by"}.issubset(cred_columns)
        finally:
            backend.close()

    def test_h1_duckdb_file_missing_principal_credential_entirely_is_not_misread_as_current(
        self, tmp_path: Path
    ) -> None:
        """See the SQLite test above - identical shape, DuckDB backend."""
        path = tmp_path / "pre_credential.duckdb"
        conn = duckdb.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT)")
        conn.close()

        report = duckdb_migrations.migrate_file(path, dry_run=True)
        assert report.from_version == 2
        assert [s.version for s in report.steps] == [3]

        with pytest.raises(SchemaError, match="format_version 2"):
            DuckDBBackend(path)

        duckdb_migrations.migrate_file(path)
        backend = DuckDBBackend(path)
        try:
            columns = {
                r[0]
                for r in backend.conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'proposal'"
                ).fetchall()
            }
            assert "reviewers" in columns
        finally:
            backend.close()

    def test_h2_stale_file_error_message_names_a_working_cli_invocation(
        self, tmp_path: Path
    ) -> None:
        """H2: the message said `ontolith db migrate --db {path}` - `--db`
        is a root-callback option and must precede the subcommand; the
        literal suggested command failed with "No such option: --db"."""
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT)"
        )
        conn.execute("CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT)")
        conn.commit()
        conn.close()

        with pytest.raises(SchemaError) as exc_info:
            SQLiteBackend(path)
        message = str(exc_info.value)
        assert f"ontolith --db {path} db migrate" in message
        assert "db migrate --db" not in message

    def test_m1_failed_migration_rolls_back_every_step_from_this_call(self, tmp_path: Path) -> None:
        """M1: a v3 failure used to leave v2's ALTER TABLEs committed with
        no format_version row recording it - half-migrated, only
        "recovering" by the accident of inference. Now the whole call is
        one transaction. `_up_v3` is monkeypatched to fail deliberately -
        the original fixture shape this test used (no `proposal` table at
        all) stopped failing once round 4's own fix taught `_up_v3` to
        tolerate its target table being absent entirely; this test's own
        purpose (atomicity across a real failure) needs a failure
        independent of that fix."""
        path = tmp_path / "will_fail.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT)"
        )
        conn.execute("CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT)")
        conn.commit()
        conn.close()

        def broken_up_v3(cursor: sqlite3.Cursor) -> None:
            raise sqlite3.OperationalError("deliberately broken")

        original = sqlite_migrations._MIGRATIONS
        sqlite_migrations._MIGRATIONS = tuple(
            sqlite_migrations._Migration(
                version=m.version,
                description=m.description,
                reversible=m.reversible,
                up=broken_up_v3 if m.version == 3 else m.up,
                down=m.down,
            )
            for m in original
        )
        try:
            with pytest.raises(StorageError, match="rolled back"):
                sqlite_migrations.migrate_file(path)
        finally:
            sqlite_migrations._MIGRATIONS = original

        conn = sqlite3.connect(str(path))
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            assert "format_version" not in tables
            columns = {r[1] for r in conn.execute("PRAGMA table_info(principal_credential)")}
            assert "issued_by" not in columns
            assert "revoked_by" not in columns
        finally:
            conn.close()

    def test_m1_failed_migration_rolls_back_every_step_from_this_call_duckdb(
        self, tmp_path: Path
    ) -> None:
        """See the SQLite test above - identical shape and reasoning,
        DuckDB backend."""
        path = tmp_path / "will_fail.duckdb"
        conn = duckdb.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT)"
        )
        conn.execute("CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT)")
        conn.close()

        def broken_up_v3(conn: duckdb.DuckDBPyConnection) -> None:
            raise duckdb.IOException("deliberately broken")

        original = duckdb_migrations._MIGRATIONS
        duckdb_migrations._MIGRATIONS = tuple(
            duckdb_migrations._Migration(
                version=m.version,
                description=m.description,
                reversible=m.reversible,
                up=broken_up_v3 if m.version == 3 else m.up,
                down=m.down,
            )
            for m in original
        )
        try:
            with pytest.raises(StorageError, match="rolled back"):
                duckdb_migrations.migrate_file(path)
        finally:
            duckdb_migrations._MIGRATIONS = original

        conn = duckdb.connect(str(path))
        try:
            tables = {
                r[0]
                for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()
            }
            assert "format_version" not in tables
            columns = {
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'principal_credential'"
                ).fetchall()
            }
            assert "issued_by" not in columns
            assert "revoked_by" not in columns
        finally:
            conn.close()

    def test_m2_migrate_file_on_current_stamped_file_takes_no_write_lock(
        self, tmp_path: Path
    ) -> None:
        """M2: calling migrate_file() on an already-current, already-stamped
        file used to still attempt a write, contending with a concurrent
        writer's own transaction - reproduced stalling for the full
        busy-timeout against a live BEGIN IMMEDIATE (KI-084) before this
        fix. Now it's read-only and returns immediately."""
        path = tmp_path / "current.db"
        backend = SQLiteBackend(path)
        backend.begin()
        try:
            start = time.monotonic()
            report = sqlite_migrations.migrate_file(path)
            elapsed = time.monotonic() - start
        finally:
            backend.rollback()
            backend.close()
        assert report.up_to_date
        assert elapsed < 1.0, f"migrate_file blocked for {elapsed:.2f}s against a live writer"

    def test_m2_connect_on_current_stamped_file_takes_no_write_lock(self, tmp_path: Path) -> None:
        """Same as above, but for the ordinary SQLiteBackend() connect path
        (require_current_format's own stamp step, not just migrate_file's)."""
        path = tmp_path / "current2.db"
        setup = SQLiteBackend(path)
        setup.close()

        writer = SQLiteBackend(path)
        writer.begin()
        try:
            start = time.monotonic()
            reader = SQLiteBackend(path)
            elapsed = time.monotonic() - start
            reader.close()
        finally:
            writer.rollback()
            writer.close()
        assert elapsed < 1.0, f"connect() blocked for {elapsed:.2f}s against a live writer"


class TestRoundTwoReviewFindings:
    """Regression test for round-2 review of ADR-0052 — reproduced a real
    bug before its fix landed; see ADR-0052's own Update section."""

    def test_partial_v2_sqlite_file_one_column_present_one_missing_still_migrates(
        self, tmp_path: Path
    ) -> None:
        """A real, reachable shape: `issued_by` present, `revoked_by`
        absent - `_infer_format_version` only distinguishes "has both" from
        "missing at least one," so it correctly calls this version 1 and
        dispatches the whole of `_up_v2`. Before the fix, `_up_v2`'s bare
        `ALTER TABLE ADD COLUMN issued_by` (issued unconditionally) raised
        `duplicate column name: issued_by` on a file shaped exactly this
        way, making it permanently un-migratable. `main`'s own old ad hoc
        fixup checked each column independently and never had this bug -
        reachable in practice because each `ALTER TABLE` there autocommits
        separately (`isolation_level=None`), so a process killed between
        the two leaves exactly this state on disk. DuckDB's `_up_v2` was
        never affected (`ADD COLUMN IF NOT EXISTS` is naturally idempotent
        per column) - this is a SQLite-only gap."""
        path = tmp_path / "partial_v2.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT, issued_by TEXT)"
        )
        conn.execute("CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT)")
        conn.commit()
        conn.close()

        report = sqlite_migrations.migrate_file(path)
        assert [s.applied for s in report.steps] == [True, True]

        backend = SQLiteBackend(path)
        try:
            columns = {
                r[1] for r in backend.conn.execute("PRAGMA table_info(principal_credential)")
            }
            assert {"issued_by", "revoked_by"}.issubset(columns)
            proposal_columns = {r[1] for r in backend.conn.execute("PRAGMA table_info(proposal)")}
            assert "reviewers" in proposal_columns
        finally:
            backend.close()

    def test_partial_v2_sqlite_file_the_other_column_present_still_migrates(
        self, tmp_path: Path
    ) -> None:
        """The complementary shape to the test above - `revoked_by`
        present, `issued_by` absent - exercising `_up_v2`'s other branch."""
        path = tmp_path / "partial_v2_other.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT, revoked_by TEXT)"
        )
        conn.execute("CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT)")
        conn.commit()
        conn.close()

        sqlite_migrations.migrate_file(path)

        backend = SQLiteBackend(path)
        try:
            columns = {
                r[1] for r in backend.conn.execute("PRAGMA table_info(principal_credential)")
            }
            assert {"issued_by", "revoked_by"}.issubset(columns)
        finally:
            backend.close()

    def test_duckdb_table_existence_checks_dont_count_a_view_as_the_table(
        self, tmp_path: Path
    ) -> None:
        """DuckDB's `information_schema.tables` lists views alongside real
        tables, unfiltered by `table_type` - a view happening to be named
        `proposal` used to read as the real table, get its (view-shaped)
        columns inspected, and then fail migration with `Can only modify
        view with ALTER VIEW` when `_up_v3` tried an `ALTER TABLE` against
        it. Fixed by filtering `table_type = 'BASE TABLE'`, matching
        SQLite's own `type = 'table'` filter, in `_table_exists`.

        Round-5 review found a further consequence worth pinning explicitly
        rather than leaving implicit in `report.up_to_date`: round 4's own
        table-existence guard (`_up_v3`'s `if not _table_exists(...):
        return`) treats a view the same as "absent" - `_table_exists`
        alone can't distinguish them - so `_up_v3` now skips itself here
        too, silently. `CREATE TABLE IF NOT EXISTS` in `_create_schema()`
        then no-ops against the existing view rather than replacing it
        (verified directly - not asserted blindly), so this reports a
        successful migration while `proposal` stays a view forever. A
        deliberately-created, pathological shape (a view named exactly
        `proposal` is not something this project's own code ever
        produces), documented on `_up_v3`'s own docstring rather than
        further guarded against."""
        path = tmp_path / "view_named_proposal.duckdb"
        conn = duckdb.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT, issued_by TEXT, revoked_by TEXT)"
        )
        conn.execute("CREATE VIEW proposal AS SELECT 1 AS id")
        conn.close()

        # Treated the same as "proposal doesn't exist" - no attempt to
        # ALTER the view, no crash.
        report = duckdb_migrations.migrate_file(path, dry_run=True)
        assert report.up_to_date

        # Explicitly pin the documented consequence: a real (non-dry-run)
        # migrate "succeeds" but leaves `proposal` a view, not a table.
        duckdb_migrations.migrate_file(path)
        conn = duckdb.connect(str(path))
        try:
            kind = conn.execute(
                "SELECT table_type FROM information_schema.tables WHERE table_name = 'proposal'"
            ).fetchone()
            assert kind == ("VIEW",)
        finally:
            conn.close()


class TestRoundThreeReviewFindings:
    """Regression tests for round-3 review of ADR-0052 — each reproduced a
    real bug before its corresponding fix landed; see ADR-0052's own Update
    section for the full record."""

    def test_up_v3_tolerates_reviewers_already_present_sqlite(self, tmp_path: Path) -> None:
        """MEDIUM: a file needing v2 (principal_credential still short a
        column) but already v3-shaped on `proposal` was inferred as version
        1 regardless of `proposal`'s own state (inference reports the
        *minimum* implied version, not a per-migration one) - `_up_v3` then
        ran unconditionally right after `_up_v2`, against a table that
        already had `reviewers`, raising `duplicate column name: reviewers`.
        Reproduces the same bug class round 2 fixed for `_up_v2`, one
        version later - round 2's own claim that `_up_v3` needed no
        equivalent fix was itself wrong."""
        path = tmp_path / "already_v3_proposal.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT, "
            "reviewers TEXT NOT NULL DEFAULT '[]')"
        )
        conn.commit()
        conn.close()

        report = sqlite_migrations.migrate_file(path)
        assert [s.applied for s in report.steps] == [True, True]

        backend = SQLiteBackend(path)
        try:
            columns = {
                r[1] for r in backend.conn.execute("PRAGMA table_info(principal_credential)")
            }
            assert {"issued_by", "revoked_by"}.issubset(columns)
        finally:
            backend.close()

    def test_duckdb_columns_of_scoped_to_main_schema_not_a_decoy_in_another_schema(
        self, tmp_path: Path
    ) -> None:
        """MEDIUM: `information_schema` spans every schema and attached
        catalog, unlike SQLite's file-scoped `sqlite_master` - a decoy
        table in a user-created schema, same name and a `reviewers` column,
        made `main.proposal`'s own still-pending v3 migration silently
        invisible. Fixed by scoping every information_schema query to
        table_schema='main' AND table_catalog=current_database()."""
        path = tmp_path / "decoy_schema.duckdb"
        conn = duckdb.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT, issued_by TEXT, revoked_by TEXT)"
        )
        conn.execute("CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT)")
        conn.execute("CREATE SCHEMA analytics")
        conn.execute("CREATE TABLE analytics.proposal (id TEXT, reviewers TEXT, status TEXT)")
        conn.close()

        report = duckdb_migrations.migrate_file(path, dry_run=True)
        assert not report.up_to_date
        assert [s.version for s in report.steps] == [3]

    def test_migrate_file_on_corrupt_sqlite_file_raises_storage_error(self, tmp_path: Path) -> None:
        """LOW: a corrupt/non-database file raised a raw sqlite3.Error from
        the read-current-version step, outside the migration transaction's
        own StorageError translation - contradicting migrate_file's own
        documented error-taxonomy claim."""
        path = tmp_path / "corrupt.db"
        path.write_text("not a database")

        with pytest.raises(StorageError, match="format_version"):
            sqlite_migrations.migrate_file(path)

    def test_migrate_file_on_corrupt_duckdb_file_raises_storage_error(self, tmp_path: Path) -> None:
        """LOW: same as above, DuckDB. duckdb.connect() itself (not just a
        later read) validates the file format eagerly and raises
        immediately for a corrupt file - needed its own translation,
        distinct from wrapping the read that follows it."""
        path = tmp_path / "corrupt.duckdb"
        path.write_text("not a database")

        with pytest.raises(StorageError, match="DuckDB database"):
            duckdb_migrations.migrate_file(path)

    def test_migrate_file_wraps_a_read_current_version_failure_after_connect_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LOW: distinct guard from the connect()-time one above - a
        failure reading the current version *after* a successful connect
        (a scenario `duckdb.connect()`'s own eager format validation makes
        hard to construct with a real file, since a file that opens cleanly
        essentially never fails a subsequent read) must also translate to
        StorageError, not a raw duckdb.Error. Monkeypatches
        read_current_version directly to simulate it, since this is a
        defensive guard for a case with no natural repro via file
        corruption alone."""
        path = tmp_path / "will_fail_on_read.duckdb"
        conn = duckdb.connect(str(path))
        conn.close()

        def broken_read(conn: duckdb.DuckDBPyConnection) -> int:
            raise duckdb.IOException("simulated read failure")

        monkeypatch.setattr(duckdb_migrations, "read_current_version", broken_read)

        with pytest.raises(StorageError, match="Could not read format_version"):
            duckdb_migrations.migrate_file(path)

    def test_failing_version_names_the_step_that_actually_failed_not_the_target(
        self, tmp_path: Path
    ) -> None:
        """LOW: a mid-sequence failure's StorageError previously always
        named the sequence's overall target version (e.g. v2 failing still
        said "to format_version 3"). Simulated by monkeypatching v2's own
        up() to fail deliberately, confirming the message names 2, not 3."""
        path = tmp_path / "v2_fails.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT)"
        )
        conn.execute("CREATE TABLE proposal (id TEXT PRIMARY KEY, namespace TEXT, author TEXT)")
        conn.commit()
        conn.close()

        def broken_up_v2(cursor: sqlite3.Cursor) -> None:
            raise sqlite3.OperationalError("deliberately broken")

        original = sqlite_migrations._MIGRATIONS
        sqlite_migrations._MIGRATIONS = tuple(
            sqlite_migrations._Migration(
                version=m.version,
                description=m.description,
                reversible=m.reversible,
                up=broken_up_v2 if m.version == 2 else m.up,
                down=m.down,
            )
            for m in original
        )
        try:
            with pytest.raises(StorageError, match="format_version 2 failed"):
                sqlite_migrations.migrate_file(path)
        finally:
            sqlite_migrations._MIGRATIONS = original


class TestRoundFourReviewFindings:
    """Regression tests for round-4 review of ADR-0052 — each reproduced a
    real bug before its corresponding fix landed; see ADR-0052's own Update
    section for the full record."""

    def test_up_v2_and_up_v3_tolerate_proposal_missing_entirely_sqlite(
        self, tmp_path: Path
    ) -> None:
        """MEDIUM: rounds 2/3 taught each up() to tolerate its own column
        already being present, but not its own target table being absent
        entirely - a file needing v2 (principal_credential still short
        both columns) with no `proposal` table at all was inferred as
        version 1 (the minimum across both independent per-table checks),
        dispatching _up_v3 right after _up_v2 against a table that doesn't
        exist (`no such table: proposal`), permanently un-migratable
        (round 1's own atomicity fix rolls the whole call back every
        retry). Fixed: both up()s now skip themselves when their own
        target table doesn't exist yet - _create_schema() creates it
        fresh, at the current shape, on the next real connect."""
        path = tmp_path / "no_proposal_at_all.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT)"
        )
        # No `proposal` table at all.
        conn.commit()
        conn.close()

        report = sqlite_migrations.migrate_file(path)
        assert [s.applied for s in report.steps] == [True, True]

        backend = SQLiteBackend(path)
        try:
            columns = {
                r[1] for r in backend.conn.execute("PRAGMA table_info(principal_credential)")
            }
            assert {"issued_by", "revoked_by"}.issubset(columns)
            proposal_columns = {r[1] for r in backend.conn.execute("PRAGMA table_info(proposal)")}
            assert "reviewers" in proposal_columns
        finally:
            backend.close()

    def test_up_v2_and_up_v3_tolerate_proposal_missing_entirely_duckdb(
        self, tmp_path: Path
    ) -> None:
        """See the SQLite test above - identical shape, DuckDB backend."""
        path = tmp_path / "no_proposal_at_all.duckdb"
        conn = duckdb.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE principal_credential (id TEXT PRIMARY KEY, principal_id TEXT, "
            "token_hash TEXT, created_at TEXT, revoked_at TEXT)"
        )
        conn.close()

        report = duckdb_migrations.migrate_file(path)
        assert [s.applied for s in report.steps] == [True, True]

        backend = DuckDBBackend(path)
        try:
            columns = {
                r[0]
                for r in backend.conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'proposal'"
                ).fetchall()
            }
            assert "reviewers" in columns
        finally:
            backend.close()

    def test_up_v2_tolerates_principal_credential_missing_entirely_via_stale_stamp_sqlite(
        self, tmp_path: Path
    ) -> None:
        """MEDIUM (the `_up_v2` half): unlike `_up_v3`'s absent-table case,
        `_up_v2` being dispatched while `principal_credential` is entirely
        absent isn't reachable through ordinary column-based inference
        alone (`_infer_format_version` can only report version 1 - forcing
        `_up_v2` into `pending` - when `principal_credential` already
        exists, since that's the only place `needs_v2` can become True) -
        but a stale/tampered `format_version` row (read_current_version
        trusts a stored row over inference, by design) that claims version
        1 against a file with *both* `principal_credential` and `proposal`
        missing entirely reaches it directly. Confirms the guard added for
        symmetry with `_up_v3` is reachable and correct, not just
        defensive dead code."""
        path = tmp_path / "tampered_stamp.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE format_version (id INTEGER PRIMARY KEY CHECK(id = 1), "
            "version INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO format_version (id, version) VALUES (1, 1)")
        conn.commit()
        conn.close()

        report = sqlite_migrations.migrate_file(path)
        assert [s.applied for s in report.steps] == [True, True]

        backend = SQLiteBackend(path)
        try:
            columns = {
                r[1] for r in backend.conn.execute("PRAGMA table_info(principal_credential)")
            }
            assert {"issued_by", "revoked_by"}.issubset(columns)
        finally:
            backend.close()

    def test_up_v2_tolerates_principal_credential_missing_entirely_via_stale_stamp_duckdb(
        self, tmp_path: Path
    ) -> None:
        """See the SQLite test above - identical shape and reasoning,
        DuckDB backend."""
        path = tmp_path / "tampered_stamp.duckdb"
        conn = duckdb.connect(str(path))
        conn.execute("CREATE TABLE principal (id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE format_version (id INTEGER PRIMARY KEY CHECK(id = 1), "
            "version INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO format_version (id, version) VALUES (1, 1)")
        conn.close()

        report = duckdb_migrations.migrate_file(path)
        assert [s.applied for s in report.steps] == [True, True]

        backend = DuckDBBackend(path)
        try:
            columns = {
                r[0]
                for r in backend.conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'principal_credential'"
                ).fetchall()
            }
            assert {"issued_by", "revoked_by"}.issubset(columns)
        finally:
            backend.close()

    def test_migrate_file_on_directory_path_raises_storage_error_sqlite(
        self, tmp_path: Path
    ) -> None:
        """LOW: sqlite3.connect() opens the file handle eagerly - a
        directory path raised a raw sqlite3.OperationalError, missed by
        round 3's fix, which only wrapped the read after a successful
        connect(). This is the SQLite twin of the connect()-time wrap round
        3 already added for DuckDB."""
        path = tmp_path / "a_directory.db"
        path.mkdir()

        with pytest.raises(StorageError, match="Could not open"):
            sqlite_migrations.migrate_file(path)
