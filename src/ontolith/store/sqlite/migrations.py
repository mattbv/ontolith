"""On-disk format-version migrations for the SQLite backend (SPEC §15, ADR-0052).

`format_version` is separate from `schema_version` (SPEC §6.4): `schema_version`
tracks a namespace's *domain* schema — concepts, predicates, their types — via
`Ontology.apply_schema()`. `format_version` tracks this backend's own DDL shape
— the tables/columns/indexes `_create_schema()` creates — independent of
anything a caller has ever applied. A brand-new file is always created at
`CURRENT_FORMAT_VERSION` directly (its `CREATE TABLE` statements already
declare the current shape); migrations in `_MIGRATIONS` below exist only for a
file written by an older build.

Two historical, previously-ad-hoc column additions are formalized here as the
first two registered migrations, retroactively numbered — both were already
shipped, idempotently, via a `PRAGMA table_info` + `ALTER TABLE` check at the
top of `_create_schema()` before this module existed:
- v2 (KI-060): `principal_credential.issued_by`/`.revoked_by`
- v3 (KI-078): `proposal.reviewers`

`SQLiteBackend.__init__` refuses to open an existing file below
`CURRENT_FORMAT_VERSION` (raises `SchemaError`) rather than silently applying
these — see ADR-0052's Decision for why. `migrate_file()` is the explicit,
standalone entry point that actually applies them (or, with `dry_run=True`,
only reports what's pending) — it opens its own connection rather than going
through `SQLiteBackend`, since the whole point is to work on a file
`SQLiteBackend.__init__` would otherwise refuse.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ontolith.core.errors import SchemaError
from ontolith.store.migrations import MigrationReport, MigrationStep

CURRENT_FORMAT_VERSION = 3
"""The SQLite backend's current on-disk format version. Bump this — and add
a new entry to `_MIGRATIONS` — whenever `_create_schema()`'s DDL changes in a
way an already-written file wouldn't pick up for free (a new column, a new
table a pre-existing deployment needs backfilled, a changed CHECK constraint
SQLite can't widen in place, etc). Purely additive `CREATE TABLE IF NOT
EXISTS`/`CREATE INDEX IF NOT EXISTS` statements for a brand-new table don't
need a version bump — they're no-ops against a file that already has
everything else, same as they are against a genuinely empty one."""


@dataclass(frozen=True)
class _Migration:
    """One registered format-version migration (internal to this module —
    callers see only `MigrationStep`/`MigrationReport`, in `store/migrations.py`)."""

    version: int
    """The format_version this migration moves the database *to* (i.e. the
    database was at `version - 1` immediately before `up()` runs)."""

    description: str
    reversible: bool
    up: Callable[[sqlite3.Cursor], None]
    down: Callable[[sqlite3.Cursor], None] | None = None


def _up_v2(cursor: sqlite3.Cursor) -> None:
    """KI-060: principal_credential gains issued_by/revoked_by."""
    cursor.execute("ALTER TABLE principal_credential ADD COLUMN issued_by TEXT")
    cursor.execute("ALTER TABLE principal_credential ADD COLUMN revoked_by TEXT")


def _down_v2(cursor: sqlite3.Cursor) -> None:
    """Reverses `_up_v2`. Any values already stored in `issued_by`/
    `revoked_by` are discarded — the inverse of an additive column migration
    can restore the prior *shape*, not data that only ever lived in the
    column being dropped."""
    cursor.execute("ALTER TABLE principal_credential DROP COLUMN issued_by")
    cursor.execute("ALTER TABLE principal_credential DROP COLUMN revoked_by")


def _up_v3(cursor: sqlite3.Cursor) -> None:
    """KI-078: proposal gains reviewers."""
    cursor.execute("ALTER TABLE proposal ADD COLUMN reviewers TEXT NOT NULL DEFAULT '[]'")


def _down_v3(cursor: sqlite3.Cursor) -> None:
    """Reverses `_up_v3`. Any reviewer assignments already recorded are
    discarded — same caveat as `_down_v2`."""
    cursor.execute("ALTER TABLE proposal DROP COLUMN reviewers")


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


def _table_exists(cursor: sqlite3.Cursor, name: str) -> bool:
    row = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _infer_format_version(cursor: sqlite3.Cursor) -> int:
    """Best-effort version for a file with no `format_version` row/table —
    every such file predates this migration framework, so its true version
    is recoverable from which columns it actually has (the same check the
    old ad hoc `_create_schema()` fixups used to decide whether to `ALTER
    TABLE`, repurposed here for detection instead of blind application).
    Assumes `principal_credential`/`proposal` already exist — only called
    for an existing file, never a brand-new one (see `read_current_version`).
    """
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(principal_credential)")}
    if "issued_by" not in columns or "revoked_by" not in columns:
        return 1
    proposal_columns = {row[1] for row in cursor.execute("PRAGMA table_info(proposal)")}
    if "reviewers" not in proposal_columns:
        return 2
    return 3


def _ensure_format_version_table(cursor: sqlite3.Cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS format_version (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            version INTEGER NOT NULL
        )
    """)


def read_current_version(cursor: sqlite3.Cursor) -> int:
    """Read the on-disk format_version without writing anything — safe to
    call in a dry run.

    Deliberately keys off table existence, not whether the *file* existed
    before this connection was opened — a caller-supplied path can already
    exist as a zero-byte placeholder (e.g. `tempfile.NamedTemporaryFile`)
    that SQLite/this project has never written to, which is exactly the
    same "nothing to migrate from" case as a path that didn't exist at all.
    No `principal_credential` table at all means an empty database either
    way: return `CURRENT_FORMAT_VERSION` directly rather than inferring
    (there's nothing to introspect yet), matching what a fresh
    `_create_schema()` call is about to create.
    """
    if not _table_exists(cursor, "principal_credential"):
        return CURRENT_FORMAT_VERSION
    if _table_exists(cursor, "format_version"):
        row = cursor.execute("SELECT version FROM format_version WHERE id = 1").fetchone()
        if row is not None:
            return int(row[0])
    return _infer_format_version(cursor)


def require_current_format(cursor: sqlite3.Cursor, *, path: Path) -> None:
    """Called from `SQLiteBackend.__init__` before any DDL runs. Raises
    `SchemaError` for an existing file below `CURRENT_FORMAT_VERSION`
    (points at `migrate_file`/`ontolith db migrate` instead of silently
    upgrading it — ADR-0052) or above it (a file written by a newer build
    than this one). Stamps/confirms the `format_version` row for an
    already-current file so future opens read it directly instead of
    re-inferring every time.
    """
    version = read_current_version(cursor)
    if version < CURRENT_FORMAT_VERSION:
        raise SchemaError(
            f"Database at {path} is format_version {version}, this build requires "
            f"{CURRENT_FORMAT_VERSION}. Run `ontolith db migrate --db {path}` first "
            "(add --dry-run to preview).",
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
    _ensure_format_version_table(cursor)
    cursor.execute(
        "INSERT INTO format_version (id, version) VALUES (1, ?) ON CONFLICT(id) DO NOTHING",
        (version,),
    )


def migrate_file(path: str | Path, *, dry_run: bool = False) -> MigrationReport:
    """Standalone migration entry point (CLI: `ontolith db migrate`).

    Deliberately does not go through `SQLiteBackend` — bypasses its
    format-version refusal, since that refusal is exactly what a stale file
    needs this function to get past. Opens its own connection, applies (or,
    with `dry_run=True`, only reports) every migration between the file's
    current version and `CURRENT_FORMAT_VERSION`, in order. A file already
    at `CURRENT_FORMAT_VERSION` returns an empty-`steps` report — safe to
    call unconditionally, e.g. before every deploy.

    Raises:
        SchemaError: `path` doesn't exist yet (nothing to migrate — connect
            once first to create a fresh database), or the file's version is
            newer than this build supports.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise SchemaError(
            f"No database file at {file_path} — nothing to migrate. Connect once "
            "first (e.g. `Ontology.connect(path)`) to create a fresh database at "
            "the current format.",
            detail={"path": str(file_path)},
        )
    conn = sqlite3.connect(str(file_path))
    try:
        cursor = conn.cursor()
        current = read_current_version(cursor)
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
            _ensure_format_version_table(cursor)
            for m in pending:
                m.up(cursor)
                cursor.execute(
                    "INSERT INTO format_version (id, version) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
                    (m.version,),
                )
            if not pending:
                cursor.execute(
                    "INSERT INTO format_version (id, version) VALUES (1, ?) "
                    "ON CONFLICT(id) DO NOTHING",
                    (current,),
                )
            conn.commit()
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
