"""On-disk format-version migrations for the DuckDB backend (SPEC §15, ADR-0052).

Mirrors `store/sqlite/migrations.py` exactly in shape and version numbering
(both backends went through the identical two historical column additions,
KI-060 and KI-078, so both land on `CURRENT_FORMAT_VERSION = 3`) — only the
DDL/introspection syntax differs: DuckDB supports `ADD COLUMN IF NOT EXISTS`
directly (no `PRAGMA table_info` existence check needed for `up()`), and
column introspection reads `information_schema.columns` instead of `PRAGMA
table_info`. See the SQLite module's docstring for the shared rationale
(why format_version is separate from schema_version, why a stale file is
refused rather than silently upgraded) — not repeated here.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import duckdb

from ontolith.core.errors import SchemaError, StorageError
from ontolith.store.migrations import MigrationReport, MigrationStep

CURRENT_FORMAT_VERSION = 3
"""See `store.sqlite.migrations.CURRENT_FORMAT_VERSION` — same meaning,
tracked independently per backend (the two happen to coincide today because
both backends have shipped the identical two historical DDL changes)."""


@dataclass(frozen=True)
class _Migration:
    version: int
    description: str
    reversible: bool
    up: Callable[[duckdb.DuckDBPyConnection], None]
    down: Callable[[duckdb.DuckDBPyConnection], None] | None = None


def _up_v2(conn: duckdb.DuckDBPyConnection) -> None:
    """KI-060: principal_credential gains issued_by/revoked_by."""
    conn.execute("ALTER TABLE principal_credential ADD COLUMN IF NOT EXISTS issued_by TEXT")
    conn.execute("ALTER TABLE principal_credential ADD COLUMN IF NOT EXISTS revoked_by TEXT")


def _down_v2(conn: duckdb.DuckDBPyConnection) -> None:
    """Reverses `_up_v2` — see the SQLite module's `_down_v2` for the same
    data-loss caveat (the inverse of an additive column migration restores
    shape, not data that only ever lived in the dropped column).

    Drops and recreates `idx_principal_credential_principal` around the two
    `DROP COLUMN`s — verified directly against the pinned duckdb version:
    `ALTER TABLE ... DROP COLUMN` unconditionally refuses when the table has
    *any* secondary index at all, even one on an unrelated column
    (`Dependency Error: Cannot alter entry "principal_credential" because
    there are entries that depend on it`) — without this, the migration
    would need `reversible=False` despite the DDL rewrite being entirely
    invertible in principle. The index name is this migration's own frozen
    historical fact (the shape a v2 database actually has), not read from
    `_create_schema()` — matches how every other migration here describes a
    fixed past state, not "whatever the schema looks like today."
    """
    conn.execute("DROP INDEX IF EXISTS idx_principal_credential_principal")
    conn.execute("ALTER TABLE principal_credential DROP COLUMN issued_by")
    conn.execute("ALTER TABLE principal_credential DROP COLUMN revoked_by")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_principal_credential_principal "
        "ON principal_credential(principal_id)"
    )


def _up_v3(conn: duckdb.DuckDBPyConnection) -> None:
    """KI-078: proposal gains reviewers.

    No `NOT NULL` on this `ADD COLUMN` (unlike the fresh `CREATE TABLE`'s own
    `reviewers TEXT NOT NULL DEFAULT '[]'`) — DuckDB's parser rejects any
    constraint (`NOT NULL`/`UNIQUE`/`CHECK` alike) on `ADD COLUMN` ("Adding
    columns with constraints not yet supported"), verified directly against
    the pinned duckdb version. A plain `DEFAULT` isn't treated as a
    constraint and DuckDB backfills every existing row with it (also
    verified directly), so this one statement both adds the column and
    leaves no row NULL — a migrated database ends up with a slightly weaker
    constraint on this one column than a fresh one gets, a pre-existing
    limitation of the DDL this migration formalizes, not a new one.
    """
    conn.execute("ALTER TABLE proposal ADD COLUMN IF NOT EXISTS reviewers TEXT DEFAULT '[]'")


def _down_v3(conn: duckdb.DuckDBPyConnection) -> None:
    """Reverses `_up_v3` — same data-loss caveat as `_down_v2`."""
    conn.execute("ALTER TABLE proposal DROP COLUMN reviewers")


_MIGRATIONS: tuple[_Migration, ...] = (
    _Migration(
        version=2,
        description="principal_credential gains issued_by/revoked_by (KI-060)",
        reversible=True,
        up=_up_v2,
        down=_down_v2,
    ),
    _Migration(
        version=3,
        description="proposal gains reviewers (KI-078)",
        reversible=True,
        up=_up_v3,
        down=_down_v3,
    ),
)
assert [m.version for m in _MIGRATIONS] == list(range(2, CURRENT_FORMAT_VERSION + 1)), (
    "_MIGRATIONS must be dense and contiguous, 2..CURRENT_FORMAT_VERSION — read/apply logic below assumes no gaps"
)


def _table_exists(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()
    return row is not None


def _any_table_exists(conn: duckdb.DuckDBPyConnection) -> bool:
    row = conn.execute("SELECT 1 FROM information_schema.tables LIMIT 1").fetchone()
    return row is not None


def _columns_of(conn: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table],
        ).fetchall()
    }


def _infer_format_version(conn: duckdb.DuckDBPyConnection) -> int:
    """See `store.sqlite.migrations._infer_format_version` — identical
    reasoning and the identical fix, DuckDB introspection: each table is
    checked independently rather than gated on `principal_credential`'s
    presence (a real, older file can have `proposal` without
    `principal_credential` at all — see the SQLite module's docstring for
    the reproduced regression this closes)."""
    needs_v2 = False
    if _table_exists(conn, "principal_credential"):
        columns = _columns_of(conn, "principal_credential")
        needs_v2 = "issued_by" not in columns or "revoked_by" not in columns
    needs_v3 = False
    if _table_exists(conn, "proposal"):
        proposal_columns = _columns_of(conn, "proposal")
        needs_v3 = "reviewers" not in proposal_columns
    if needs_v2:
        return 1
    if needs_v3:
        return 2
    return 3


def _ensure_format_version_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS format_version (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            version INTEGER NOT NULL
        )
    """)


def read_current_version(conn: duckdb.DuckDBPyConnection) -> int:
    """See `store.sqlite.migrations.read_current_version` — identical
    contract (keyed off whether any table at all exists, not file
    existence — a pre-created zero-byte placeholder path is the same
    "nothing to migrate from" case as a path that didn't exist at all;
    "any table" rather than one specific table matters for the same reason
    named in `_infer_format_version`'s docstring), DuckDB introspection.
    Safe to call in a dry run (no writes)."""
    if not _any_table_exists(conn):
        return CURRENT_FORMAT_VERSION
    if _table_exists(conn, "format_version"):
        row = conn.execute("SELECT version FROM format_version WHERE id = 1").fetchone()
        if row is not None:
            return int(row[0])
    return _infer_format_version(conn)


def require_current_format(conn: duckdb.DuckDBPyConnection, *, path: Path) -> None:
    """See `store.sqlite.migrations.require_current_format` — identical
    contract, called from `DuckDBBackend.__init__` before any DDL runs.
    Skips the stamp write entirely when a correct row already exists (same
    reasoning: avoid taking a write lock on the common already-current
    case)."""
    version = read_current_version(conn)
    if version < CURRENT_FORMAT_VERSION:
        raise SchemaError(
            f"Database at {path} is format_version {version}, this build requires "
            f"{CURRENT_FORMAT_VERSION}. Run the DuckDB equivalent of "
            "`ontolith db migrate` first (add --dry-run to preview) — see "
            "ontolith.store.duckdb.migrations.migrate_file.",
            detail={
                "path": str(path),
                "from_version": version,
                "to_version": CURRENT_FORMAT_VERSION,
            },
        )
    if version > CURRENT_FORMAT_VERSION:
        raise SchemaError(
            f"Database at {path} is format_version {version}, newer than this build's "
            f"{CURRENT_FORMAT_VERSION} — it was written by a newer version of Ontolith. "
            "Upgrade before opening this file.",
            detail={
                "path": str(path),
                "from_version": version,
                "to_version": CURRENT_FORMAT_VERSION,
            },
        )
    if _table_exists(conn, "format_version"):
        row = conn.execute("SELECT version FROM format_version WHERE id = 1").fetchone()
        if row is not None and int(row[0]) == version:
            return
    _ensure_format_version_table(conn)
    conn.execute(
        "INSERT INTO format_version (id, version) VALUES (1, ?) ON CONFLICT (id) DO NOTHING",
        [version],
    )


def migrate_file(path: str | Path, *, dry_run: bool = False) -> MigrationReport:
    """Standalone migration entry point for a DuckDB file. See
    `store.sqlite.migrations.migrate_file` — identical contract (one
    transaction covering every pending `up()` plus the stamp, rolled back
    and re-raised as `StorageError` on a driver failure; no write at all
    when the file is already current and already stamped); this backend has
    no CLI command yet (`Ontology.connect()`/the CLI are SQLite-only today,
    ADR-0034's own scoping), so this is reached programmatically.

    Unlike the SQLite module, this is *not* fully isolated from a live
    `DuckDBBackend` on the same path within the same process: DuckDB's
    Python client returns the same underlying instance for two same-process
    connections to the same file (verified directly — a second connection's
    `ALTER TABLE` was immediately visible to, and bypassed the `threading.RLock`
    of, a live `DuckDBBackend` on that path). Only reachable when the file is
    already current (a stale one is refused before `DuckDBBackend.__init__`
    ever returns, so there's no live in-process backend on a stale file to
    race in the first place); a cross-process caller gets DuckDB's own file
    lock instead (`IOException`, not silently interleaved).

    Raises:
        SchemaError: `path` doesn't exist yet, or the file's version is
            newer than this build supports.
        StorageError: a migration's `up()` (or the format_version stamp)
            failed against the driver; every change from this call is
            rolled back first.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise SchemaError(
            f"No database file at {file_path} — nothing to migrate. Connect once "
            "first (e.g. `DuckDBBackend(path)`) to create a fresh database at the "
            "current format.",
            detail={"path": str(file_path)},
        )
    conn = duckdb.connect(str(file_path))
    try:
        current = read_current_version(conn)
        if current > CURRENT_FORMAT_VERSION:
            raise SchemaError(
                f"Database at {file_path} is format_version {current}, newer than this "
                f"build's {CURRENT_FORMAT_VERSION} — it was written by a newer version "
                "of Ontolith. Upgrade before migrating this file.",
                detail={
                    "path": str(file_path),
                    "from_version": current,
                    "to_version": CURRENT_FORMAT_VERSION,
                },
            )
        pending = [m for m in _MIGRATIONS if m.version > current]
        steps = tuple(
            MigrationStep(
                version=m.version,
                description=m.description,
                reversible=m.reversible,
                applied=not dry_run,
            )
            for m in pending
        )
        if not dry_run:
            already_stamped = False
            if _table_exists(conn, "format_version"):
                row = conn.execute("SELECT version FROM format_version WHERE id = 1").fetchone()
                already_stamped = row is not None and int(row[0]) == current
            if pending or not already_stamped:
                final_version = pending[-1].version if pending else current
                try:
                    conn.execute("BEGIN")
                    _ensure_format_version_table(conn)
                    for m in pending:
                        m.up(conn)
                    conn.execute(
                        "INSERT INTO format_version (id, version) VALUES (1, ?) "
                        "ON CONFLICT (id) DO UPDATE SET version = excluded.version",
                        [final_version],
                    )
                    conn.commit()
                except duckdb.Error as e:
                    conn.rollback()
                    raise StorageError(
                        f"Migration to format_version {final_version} failed, rolled back: {e}"
                    ) from e
        return MigrationReport(
            from_version=current,
            to_version=CURRENT_FORMAT_VERSION,
            steps=steps,
            dry_run=dry_run,
        )
    finally:
        conn.close()


__all__ = [
    "CURRENT_FORMAT_VERSION",
    "migrate_file",
    "read_current_version",
    "require_current_format",
]
