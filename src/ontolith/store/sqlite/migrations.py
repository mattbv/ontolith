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

from ontolith.core.errors import SchemaError, StorageError
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
    """The format_version this migration moves the database *to*. Not
    necessarily *from* `version - 1` exactly — `read_current_version`
    reports the minimum version implied across every table's own
    independent check, so a file can be at an inferred version below this
    migration's own table-specific "already done" state (round-3 review:
    `_up_v2`'s and `_up_v3`'s own per-column checks are what make running
    against an already-current table a safe no-op rather than an error)."""

    description: str
    reversible: bool
    up: Callable[[sqlite3.Cursor], None]
    down: Callable[[sqlite3.Cursor], None] | None = None


def _columns_of(cursor: sqlite3.Cursor, table: str) -> set[str]:
    """Column names on `table`, via `PRAGMA table_info`.

    `table` is f-string-interpolated (`PRAGMA` doesn't accept bound
    parameters) — safe today because every call site in this module passes
    a hardcoded literal (`"principal_credential"`, `"proposal"`), never a
    caller-controlled value; there is no public entry point into this
    private helper.
    """
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}


def _up_v2(cursor: sqlite3.Cursor) -> None:
    """KI-060: principal_credential gains issued_by/revoked_by.

    Checks each column independently before its own `ALTER TABLE`, rather
    than assuming neither exists yet — round-2 review found a real,
    reachable partial-v2 shape (one column added, the other not) that a
    bare pair of `ALTER TABLE ADD COLUMN`s can't tolerate: `main`'s own old
    ad hoc fixup ran each `ALTER TABLE` as its own autocommit statement
    (`isolation_level=None`), so a process killed between the two leaves
    exactly this shape on disk, and `_infer_format_version` — which only
    distinguishes "has both" from "missing at least one," not which one —
    correctly calls that version 1, then dispatches the whole of `_up_v2`
    unconditionally. Reproduced: `issued_by` present, `revoked_by` absent,
    inferred version 1, `_up_v2`'s first statement raised `duplicate column
    name: issued_by` before ever reaching the second. Fixed by checking
    columns here instead of relying on the caller's coarser detection.
    """
    columns = _columns_of(cursor, "principal_credential")
    if "issued_by" not in columns:
        cursor.execute("ALTER TABLE principal_credential ADD COLUMN issued_by TEXT")
    if "revoked_by" not in columns:
        cursor.execute("ALTER TABLE principal_credential ADD COLUMN revoked_by TEXT")


def _down_v2(cursor: sqlite3.Cursor) -> None:
    """Reverses `_up_v2`. Any values already stored in `issued_by`/
    `revoked_by` are discarded — the inverse of an additive column migration
    can restore the prior *shape*, not data that only ever lived in the
    column being dropped."""
    cursor.execute("ALTER TABLE principal_credential DROP COLUMN issued_by")
    cursor.execute("ALTER TABLE principal_credential DROP COLUMN revoked_by")


def _up_v3(cursor: sqlite3.Cursor) -> None:
    """KI-078: proposal gains reviewers.

    Checks column existence first, same reasoning as `_up_v2` — round-3
    review found the previous version of this docstring's own claim
    ("inference already reports version 3 if `reviewers` exists, so this
    is never dispatched against a file that already has it") false:
    `_infer_format_version` reports the *minimum* version implied across
    its two independent per-table checks, not a per-migration one, so a
    file needing v2 (`principal_credential` still short a column) but
    already v3-shaped on `proposal` is inferred as version 1 regardless —
    `_up_v3` then runs unconditionally right after `_up_v2`, against a
    table that already has `reviewers`. Reproduced: exactly the same
    `duplicate column name` failure round 2 fixed for `_up_v2`, one version
    later. Every registered `up()` must tolerate its own change already
    being present, not just the one motivating case that happened to be
    reproduced first — `_up_v2`'s fix generalizes here.
    """
    if "reviewers" not in _columns_of(cursor, "proposal"):
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


def _any_table_exists(cursor: sqlite3.Cursor) -> bool:
    row = cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone()
    return row is not None


def _infer_format_version(cursor: sqlite3.Cursor) -> int:
    """Best-effort version for a file with no `format_version` row/table —
    every such file predates this migration framework, so its true version
    is recoverable from which columns it actually has (the same check the
    old ad hoc `_create_schema()` fixups used to decide whether to `ALTER
    TABLE`, repurposed here for detection instead of blind application).

    Only called for a file with at least one table (see `read_current_version`)
    — but that doesn't guarantee `principal_credential` specifically exists.
    A real, older file can have `proposal` (present since M1) without
    `principal_credential` at all (added later, KI-060's own predecessor
    commit) — round-1 review of this ADR caught this exact shape being
    misread as fully current, silently leaving `proposal.reviewers` missing
    forever, reproduced end to end. `principal_credential` missing *entirely*
    needs no `up()` of its own to fix: `_create_schema()`'s
    `CREATE TABLE IF NOT EXISTS` creates it fresh, at the current shape,
    unconditionally, on every connect that reaches it — only a column
    missing from a table that already exists needs an explicit `ALTER
    TABLE`. So each table is checked independently, not gated on the other's
    presence.
    """
    needs_v2 = False
    if _table_exists(cursor, "principal_credential"):
        columns = _columns_of(cursor, "principal_credential")
        needs_v2 = "issued_by" not in columns or "revoked_by" not in columns
    needs_v3 = False
    if _table_exists(cursor, "proposal"):
        proposal_columns = _columns_of(cursor, "proposal")
        needs_v3 = "reviewers" not in proposal_columns
    if needs_v2:
        return 1
    if needs_v3:
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

    Deliberately keys off *whether any table at all exists*, not whether the
    *file* existed before this connection was opened — a caller-supplied
    path can already exist as a zero-byte placeholder (e.g.
    `tempfile.NamedTemporaryFile`) that SQLite/this project has never
    written to, which is exactly the same "nothing to migrate from" case as
    a path that didn't exist at all: return `CURRENT_FORMAT_VERSION`
    directly rather than inferring (there's nothing to introspect yet),
    matching what a fresh `_create_schema()` call is about to create.

    Checking "any table" rather than one specific table (`principal_credential`,
    the original — and buggy — version of this check) matters: a real,
    older file can have some tables without having that one specifically
    (see `_infer_format_version`'s own docstring) — misreading that as
    "empty" would silently skip every pending migration.
    """
    if not _any_table_exists(cursor):
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
    re-inferring every time — skipped when a correct row already exists, so
    the common (already-stamped, already-current) case takes no write lock
    at all, only ever reading. An unconditional `INSERT ... ON CONFLICT DO
    NOTHING` still asks SQLite for a write lock even when the conflict
    means nothing ends up changing — reproduced contending against a live
    `transaction()` (`BEGIN IMMEDIATE`, KI-084): the unconditional version
    blocked for the full busy-timeout and then raised `OperationalError:
    database is locked` on every ordinary connect, not just a migration.
    """
    version = read_current_version(cursor)
    if version < CURRENT_FORMAT_VERSION:
        raise SchemaError(
            f"Database at {path} is format_version {version}, this build requires "
            f"{CURRENT_FORMAT_VERSION}. Run `ontolith --db {path} db migrate` first "
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
    if _table_exists(cursor, "format_version"):
        row = cursor.execute("SELECT version FROM format_version WHERE id = 1").fetchone()
        if row is not None and int(row[0]) == version:
            return
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
    current version and `CURRENT_FORMAT_VERSION`, in order, inside one
    transaction — a mid-sequence failure (a later migration's `up()` raising)
    rolls back every earlier `up()` in the same call too, rather than
    leaving the file half-migrated with no `format_version` row recording
    what actually landed (reproduced before this fix: a failing v3 left v2's
    `ALTER TABLE`s committed but no tracking row at all). A driver-level
    failure surfaces as `StorageError`, not a raw `sqlite3.Error`, matching
    every other backend method's SPEC §16 error-taxonomy convention. A file
    already at `CURRENT_FORMAT_VERSION` and already stamped takes no write
    at all (only reads) — safe to call unconditionally, e.g. before every
    deploy, including one contending with another connection's own
    transaction on the same file.

    Raises:
        SchemaError: `path` doesn't exist yet (nothing to migrate — connect
            once first to create a fresh database), or the file's version is
            newer than this build supports.
        StorageError: reading the current version failed against the driver
            (e.g. `path` exists but isn't a valid SQLite file), or a
            migration's `up()` (or the format_version stamp) failed; every
            change from this call is rolled back first.
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
        # round-3 review: a corrupt/non-database file raised a raw
        # sqlite3.DatabaseError here, contradicting this function's own
        # documented "surfaces as StorageError, not a raw sqlite3.Error"
        # claim, which previously only covered the migration transaction
        # below, not this read.
        try:
            current = read_current_version(cursor)
        except sqlite3.Error as e:
            raise StorageError(f"Could not read format_version from {file_path}: {e}") from e
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
            if _table_exists(cursor, "format_version"):
                row = cursor.execute("SELECT version FROM format_version WHERE id = 1").fetchone()
                already_stamped = row is not None and int(row[0]) == current
            if pending or not already_stamped:
                final_version = pending[-1].version if pending else current
                # Tracks which specific step was running when a driver error
                # hits, not just the sequence's overall target — round-2
                # review found the error otherwise always named
                # `final_version` regardless of which earlier step actually
                # failed, misleading for a multi-step sequence.
                failing_version = final_version
                try:
                    cursor.execute("BEGIN")
                    _ensure_format_version_table(cursor)
                    for m in pending:
                        failing_version = m.version
                        m.up(cursor)
                    cursor.execute(
                        "INSERT INTO format_version (id, version) VALUES (1, ?) "
                        "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
                        (final_version,),
                    )
                    conn.commit()
                except sqlite3.Error as e:
                    conn.rollback()
                    raise StorageError(
                        f"Migration to format_version {failing_version} failed, rolled back: {e}"
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
