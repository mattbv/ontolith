# ADR-0052: On-Disk Storage-Format Migrations

**Status:** Accepted

**Date:** 2026-09-20

**Deciders:** Ontolith Core Team

## Context

SPEC §15 (Versioning & migration) makes three distinct promises: the on-disk format carries a
`format_version`; breaking changes ship with a declared rewrite strategy and a dry-run mode;
migrations MUST be reversible or explicitly marked irreversible. None of that existed before this
ADR. `schema_version` (SPEC §6.4, `Ontology.apply_schema()`) tracks a namespace's *domain* schema —
concepts, predicates, their types — and is unrelated: it says nothing about this backend's own DDL
shape (the tables/columns/indexes `_create_schema()` creates), which is what SPEC §15 is actually
about.

In practice, the backend's own DDL *has* changed twice since M1 shipped — KI-060 added
`principal_credential.issued_by`/`.revoked_by`, KI-078 added `proposal.reviewers` — and both
changes were handled the same ad hoc way in both `SQLiteBackend`/`DuckDBBackend`: a
`PRAGMA table_info`/`information_schema.columns` existence check at the top of `_create_schema()`,
followed by an idempotent `ALTER TABLE ... ADD COLUMN` if the column was missing. This worked, but
satisfied none of SPEC §15's three requirements: there was no `format_version` to report, no
distinct dry-run step (the "migration" ran unconditionally on every connect, silently, mixed in
with ordinary schema setup), and no declared reversibility — undoing either change meant hand-editing
the database file.

ADR-0034 (the `ontolith schema migrate` CLI command) explicitly scoped itself away from this exact
problem, naming it "scope (b)" and deferring it: "Data migration/backfill ... remains explicitly out
of scope, tracked as its own future decision, not folded into this command." This ADR is that
decision — though it turns out to be about the storage layer's own DDL evolution, not (yet) about
rewriting *domain* assertion data for a renamed/retyped predicate, which ADR-0034's scope (b) was
really gesturing at. That remains a distinct, still-open problem (see Consequences).

## Decision

**`format_version` is a small, single-row table (`format_version(id INTEGER PRIMARY KEY CHECK(id =
1), version INTEGER NOT NULL)`), tracked independently per backend.** `CURRENT_FORMAT_VERSION = 3`
on both `SQLiteBackend` and `DuckDBBackend` today (both have shipped the identical two historical
column additions, so the numbers coincide — nothing requires them to stay in sync going forward).
Each backend owns its own registry: `store/sqlite/migrations.py`, `store/duckdb/migrations.py` — the
DDL is inherently backend-specific (SQLite's `up()`s issue a bare `ALTER TABLE ADD COLUMN`, relying
on each migration only ever running once per file, in order; DuckDB supports `ADD COLUMN IF NOT
EXISTS` directly but rejects any constraint on it) and version *inference* for a file with no
tracking row yet is column-existence-based on both (`PRAGMA table_info` on SQLite,
`information_schema.columns` on DuckDB), so there is no cross-backend `StorageBackend` port method
for this — see
Alternatives.

**A brand-new (empty) database file is always created directly at `CURRENT_FORMAT_VERSION`** — its
`CREATE TABLE` statements already declare the current shape, so there is nothing to migrate *from*.
"Empty" is judged by whether *any* table exists at all, not by whether the file path existed before
this connection opened it — a caller-supplied path can already exist as a zero-byte placeholder
(`tempfile.NamedTemporaryFile(delete=False)`, which several existing test fixtures use, and which
this ADR's own review round caught breaking on first pass), and that case must read the same as a
path that didn't exist at all. (An earlier version of this check keyed "empty" off one specific
table, `principal_credential` — round-1 review found and fixed a real bug that shape caused; see
this ADR's own Update section.)

**`SQLiteBackend.__init__`/`DuckDBBackend.__init__` refuse (raise `SchemaError`) to open an
existing file below `CURRENT_FORMAT_VERSION`**, rather than silently applying pending migrations the
way the old ad hoc fixups did. The check runs before any DDL touches the file, and closes the
connection before propagating. `SchemaError`'s own docstring already names "Breaking schema change
without migration" as a use case (SPEC §16) — no new error type needed. They also refuse a file
*above* `CURRENT_FORMAT_VERSION` (written by a newer Ontolith build than this one), with a distinct
message.

**Migrating is a separate, explicit, standalone action — `migrate_file(path, *, dry_run=False)` —
not something a connect ever does implicitly.** It does not go through `SQLiteBackend`/
`DuckDBBackend` at all: it opens its own raw connection, since the entire point is to work on a file
the normal constructor just refused. (DuckDB's Python client returns the *same* underlying instance
for two same-process connections to one path, so on that backend specifically this isn't full
process-level isolation from a live `DuckDBBackend` — only reachable on an already-current file
anyway, since a stale one has no live in-process backend to race in the first place; see
`duckdb/migrations.py`'s own `migrate_file` docstring.) `dry_run=True` reports every pending migration
(`MigrationReport`/`MigrationStep`, `store/migrations.py`, shared shape across both backends)
without writing anything at all — not even creating the `format_version` table itself. A file
already at `CURRENT_FORMAT_VERSION` and already stamped takes no write at all when
`dry_run=False` either — only reads — so calling `migrate_file` is safe unconditionally (e.g.
before every deploy), including one racing a concurrent writer's own transaction on the same file
(round-1 review reproduced the earlier, unconditional-write version of this stalling for the full
busy-timeout against a live `BEGIN IMMEDIATE`, KI-084, then raising `database is locked` — fixed by
skipping the write entirely once the row is confirmed already correct). Applying a *pending*
migration still needs the same write-contention care any other write does — this only removes the
lock attempt from the common no-op case. The CLI exposes this as `ontolith db status`
(read-only preview) and `ontolith db migrate [--dry-run]` — SQLite only, since `Ontology.connect()`/
the CLI are SQLite-only today (ADR-0034's own scoping); `DuckDBBackend`'s `migrate_file` is reached
programmatically.

**Applying every pending migration is one transaction, not one per step.** `migrate_file` runs the
whole pending sequence — every `up()`, in order, plus the final `format_version` stamp — inside one
explicit `BEGIN`/`commit()`, catching a driver-level failure and `rollback()`ing before re-raising as
`StorageError` (SPEC §16's own taxonomy, not a raw `sqlite3.Error`/`duckdb.Error`). A mid-sequence
failure therefore leaves the file exactly as it was before the call, never half-migrated with
earlier steps silently committed and no record of what actually landed (round-1 review reproduced
the earlier, per-statement-autocommit version of this leaving exactly that half-migrated state).

**Each migration declares `reversible: bool` and, when true, a `down()`.** Both historical
migrations are reversible on SQLite (SQLite supports `ALTER TABLE ... DROP COLUMN` since 3.35,
confirmed against the pinned interpreter). On DuckDB, reversing KI-060's `principal_credential`
migration needs to drop and recreate `idx_principal_credential_principal` around the `DROP COLUMN`
calls — DuckDB (pinned `duckdb==1.5.4`, verified directly) refuses `ALTER TABLE ... DROP COLUMN`
outright when the table has *any* secondary index, even one on an unrelated column. `proposal` has
no such index, so KI-078's reversal needs no workaround. A migration's `down()` describes a fixed
historical fact (the shape a file at that version actually has, and the index name that existed at
the time), not something that tracks `_create_schema()`'s current definition — it is not expected to
stay in sync with later schema changes, the same way the `up()` side never was either.

**Version inference for a file with no `format_version` row is column-based, not
row-count/checksum-based** — the same `PRAGMA table_info`/`information_schema.columns` check the old
ad hoc fixups used to decide *whether* to `ALTER TABLE`, repurposed here to *detect* rather than
blindly apply. Every such file necessarily predates this migration framework (any file this
framework itself has touched already has the row); inference stamps the row on first open so later
opens read it directly.

## Rationale

**Why refuse rather than keep auto-migrating silently on connect (the pre-existing behavior):** SPEC
§15's "dry-run mode" phrase only makes sense as a *preview of a distinct, deliberate action* — if
migrating still happened automatically on every connect regardless, there would be nothing to
preview separately from just connecting. Pre-1.0, with no real deployments yet depending on the old
silent-upgrade behavior, this is the right point to establish the explicit, "nothing silent"
contract SPEC §15 and this project's broader governance philosophy (conflict handling, capability
enforcement) already both favor over 1.0 freezing it the other way.

**Why not a `StorageBackend` port method (`backend.migrate()`):** the port's methods all assume an
already-constructed, already-current backend object — but the entire scenario `migrate_file` exists
for is a file the constructor just refused to build one against. Forcing this into the instance-method
contract would mean either weakening the constructor's refusal (defeating the point) or adding a
second, parallel "partially-constructed" backend state solely to host one method — more machinery for
no real gain over a module-level function, and it would make every third-party `StorageBackend`
implementation (the port has no default construction contract already — there is no
`StorageBackend.connect()` factory either) responsible for a capability this ADR doesn't require of
it.

**Why version-inference by column existence, not a schema hash/checksum:** the two migrations this
framework starts with are both simple additive column changes with a small, known, enumerable
fingerprint (does this specific column exist). A hash-based approach would need the same amount of
backend-specific detail work today and would not obviously generalize better to a future migration
that isn't column-shaped (e.g. widening a `CHECK` constraint) — deferred until a real migration shape
actually needs it, not built preemptively.

## Consequences

**Positive:**
- Closes the on-disk-format-version half of SPEC §15 entirely: `format_version` exists, is
  reported (`ontolith db status`), migrations declare reversibility, and applying one is a distinct,
  previewable, deliberate step.
- The two historical ad hoc fixups are gone from `_create_schema()` — replaced by an explicit,
  testable, independently reasoned-about registry.
- `ontolith db migrate`/`ontolith db status` give an operator a real answer to "is this database
  file current" without reading source.

**Negative / follow-ups:**
- **Breaking**: a database file below `CURRENT_FORMAT_VERSION` that previously opened silently
  (auto-migrated in place) now raises `SchemaError` until `migrate_file`/`ontolith db migrate` runs
  explicitly. Any deployment relying on the old silent-upgrade behavior needs a `db migrate` step
  added to its upgrade procedure.
- **SPEC §15's "rewrite strategy" language, and this ADR's own Decision, are still scoped to this
  backend's own DDL shape — not to rewriting *domain* assertion data for a renamed/retyped
  predicate.** ADR-0034's scope (b) — reconciling already-stored assertion data against a schema
  change (a renamed predicate, a retyped `value_type`) — remains entirely open. That problem also
  touches append-only semantics directly (old and new predicate names would need to coexist under
  some reconciliation strategy) and needs its own design, not something this ADR's DDL-focused
  mechanism happens to solve by extension.
- **DuckDB's backend has no CLI surface for this at all today** (`Ontology.connect()`/the CLI are
  SQLite-only, ADR-0034's own precedent) — `duckdb_migrations.migrate_file` is reachable only
  programmatically. Not a new gap this ADR introduces; DuckDB has no CLI surface for anything else
  either.
- **No conformance vector** — this is internal storage-adapter machinery (DDL shape, not the
  observable domain behavior SPEC §19's conformance kit exercises), covered by unit tests
  (`tests/unit/test_storage_migrations.py`, plus one test each in `test_sqlite_backend.py`/
  `test_duckdb_backend.py` exercising the same path against KI-060/KI-078's actual historical
  fixture shapes) — matches ADR-0034's own precedent for `schema show`/`schema migrate`.
- **No timeout/locking story for a concurrent `migrate_file` racing a live connect** — not addressed
  here; matches this project's existing posture that operational/deployment sequencing (don't run
  `db migrate` against a file another process has open) is the operator's responsibility, the same
  boundary KI-084's `BEGIN IMMEDIATE` fix already draws for ordinary writes.

## Alternatives Considered

- **Keep silently auto-migrating on every connect** (today's ad hoc behavior, generalized into a
  registry instead of hand-written fixups): rejected — see Rationale; doesn't satisfy SPEC §15's
  dry-run implication and keeps the "nothing silent" governance philosophy inconsistent between this
  layer and every other conflict/capability decision in the project.
- **A `StorageBackend.migrate()` port method**: rejected — see Rationale; the scenario needing this
  is exactly the one where a normal backend object can't be constructed in the first place.
- **Checksum/hash-based version detection** instead of column-existence inference: rejected for now
  — no simpler for the two migrations that exist today, deferred until a migration shape that
  genuinely needs it.
- **Fold this into `ontolith schema migrate`** (reuse the existing command name): rejected — that
  command is explicitly scoped (ADR-0034) to a namespace's *domain* schema via `apply_schema`, a
  different mechanism and a different kind of "version" than this backend's own DDL shape; conflating
  the two under one command would blur exactly the distinction ADR-0034 went out of its way to draw.

## Update (2026-09-21): two review rounds, five real findings fixed, code correct from round 3

**Round 1** (architecture + a dedicated review of this ADR's own diff) found three HIGH and two
MEDIUM issues, all reproduced by direct execution before being trusted, all fixed and re-verified:

- **HIGH — version inference misread a real, older file as fully current, permanently losing a
  pending migration.** `read_current_version`/`_infer_format_version` originally keyed "empty
  database" off `principal_credential` existing at all — but that table postdates `proposal` (added
  later, per git history), so a genuine older file (`proposal` present without `reviewers`,
  `principal_credential` absent entirely) was misread as empty, stamped `format_version=3`
  immediately, and left `proposal.reviewers` missing with no error until a query against it finally
  failed. Fixed: `_any_table_exists` now gates the "genuinely empty" fast path (any table, not one
  specific table), and `_infer_format_version` checks each table's own columns independently rather
  than gating one on the other's presence.
- **HIGH — the `SchemaError` remediation message named a command that doesn't work.** It read
  `` `ontolith db migrate --db {path}` ``; `--db` is a root-level Typer option and must precede the
  subcommand, so the literal suggested command failed with `No such option: --db`. Fixed to
  `` `ontolith --db {path} db migrate` ``.
- **HIGH — every CLI command besides `db status`/`db migrate` surfaced a raw, unhandled traceback
  against a stale file.** `_kb()` was called outside each command's own `try:`/`except Exception`
  block; `Ontology.connect()` now genuinely raises (`SchemaError`) where it previously never did for
  an openable file, and that exception escaped the CLI's own `Error: ...`/exit-1 convention entirely.
  Fixed centrally in `_kb()` itself, once, rather than in all ~25 command bodies.
- **MEDIUM — `migrate_file` applied each pending migration statement-by-statement, not atomically.**
  A mid-sequence failure left the file half-migrated (earlier steps' DDL already committed) with no
  `format_version` row recording what actually landed, and a raw `sqlite3.Error`/`duckdb.Error`
  escaped instead of this project's `StorageError` taxonomy. Fixed: the whole pending sequence plus
  the final stamp now runs inside one explicit transaction, rolled back and re-raised as
  `StorageError` on failure — recorded as its own Decision bullet above.
- **MEDIUM — `migrate_file`/`require_current_format` still attempted a write on the common no-op
  case.** An unconditional `INSERT ... ON CONFLICT DO NOTHING` still asks for a write lock even when
  nothing changes — reproduced stalling for the full busy-timeout against a live `BEGIN IMMEDIATE`
  (KI-084) before raising `database is locked`, contradicting the "safe to call unconditionally"
  claim for the exact deploy scenario it names. Fixed: both now skip the write entirely once a
  matching row is confirmed already present.
- Two LOWs from the same round: `SQLiteBackend.__init__`'s `PRAGMA journal_mode = WAL` ran *before*
  the format-version refusal, mutating a stale file's on-disk journal mode ahead of declining to open
  it — moved the refusal earlier, ahead of every PRAGMA. Both backends' refusal-path cleanup only
  caught `SchemaError`, leaking a connection on a genuinely corrupt/non-database file (a different
  exception type) — broadened to `except BaseException`.

**Round 2** independently re-verified every round-1 fix by direct reproduction and mutation testing
(reverting each fix in turn and confirming its regression test fails) — all five held. It found one
further real bug, reachable specifically because of round 1's own fix:

- **MEDIUM — a partially-applied v2 file (one of the two v2 columns present, the other missing) was
  permanently un-migratable on SQLite.** `_infer_format_version` only distinguishes "has both
  columns" from "missing at least one" — it can't tell *which* one is missing — so it correctly
  reports version 1 and dispatches the whole of `_up_v2`. `_up_v2`'s bare `ALTER TABLE ADD COLUMN
  issued_by` (unconditional) then raised `duplicate column name: issued_by` before ever reaching
  `revoked_by`, and — because of round 1's own atomicity fix — the whole call rolled back and raised
  `StorageError`, with no path forward short of manual SQL. Reachable in practice: `main`'s own old ad
  hoc fixup ran each `ALTER TABLE` as its own autocommit statement, so a process killed between the
  two leaves exactly this shape on disk. DuckDB's `_up_v2` was never affected — `ADD COLUMN IF NOT
  EXISTS` is naturally idempotent per column — this was a SQLite-only gap. Fixed: `_up_v2` now checks
  each column's own existence before its own `ALTER TABLE`, the same way the old ad hoc fixup did;
  `_up_v3` needs no equivalent fix (a single-column migration has no partial state for inference to
  under-detect in the first place — documented on its own docstring).
- Also found and fixed two documentation-accuracy issues from round 1's own docs commit: the Decision
  section still described "empty" as keyed off `principal_credential` specifically (the exact rule
  round 1's own fix replaced) instead of "any table"; and the atomicity guarantee round 1's fix
  actually built (one transaction, `StorageError` on failure) was never stated anywhere in this ADR's
  prose at all, only in the code — now recorded as its own Decision bullet.

Every claim in this Update section was independently re-verified against source and by direct
execution before being written, not carried forward from either review round's own report.

## References

- SPEC §15 (Versioning & migration — the three requirements this ADR implements)
- SPEC §6.4 (`schema_version` — the distinct, unrelated domain-schema versioning this ADR does not
  touch)
- SPEC §16 (Error model — `SchemaError`'s "Breaking schema change without migration" example, reused
  here rather than a new error type)
- ADR-0034 (`ontolith schema migrate` — explicitly deferred this exact problem as "scope (b)"; this
  ADR is that follow-up, though narrower than ADR-0034's own framing — DDL shape, not domain data)
- `docs/known-issues.md` KI-060 (`principal_credential.issued_by`/`.revoked_by`, the v2 migration),
  KI-078 (`proposal.reviewers`, the v3 migration) — both retroactively formalized here
- `docs/Ontolith_Implementation_Plan.md` §2 (M4 milestone table — "migration tooling," this ADR's
  workstream, and its "on-disk `format_version` frozen" exit criterion, which this ADR makes
  meaningful for the first time by giving `format_version` something to freeze) and §7.2 (Branching
  & releases, the same `format_version`-frozen-at-1.0 commitment stated again there)
