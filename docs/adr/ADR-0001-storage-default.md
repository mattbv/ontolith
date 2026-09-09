# ADR-0001: Storage Default (SQLite + sqlite-vec)

**Status:** Accepted

**Date:** 2026-06-20

**Deciders:** Ontolith Core Team

## Context

We need a default storage backend that:
- Requires zero external infrastructure (local-first)
- Supports both symbolic graph queries and vector search
- Can scale from laptop to production
- Is stable and widely deployed

The assertion-centric model maps naturally to rows-with-metadata, but we also need hybrid (symbolic + vector) retrieval for grounding.

## Decision

**Default to SQLite-backed assertion store + sqlite-vec for embeddings**, with pluggable scale-out backends.

- **Storage**: Single SQLite file with WAL mode
- **Vector search**: sqlite-vec (embedded in same file)
- **Pluggability**: Clean `StorageBackend` port for swapping (DuckDB+DuckPGQ, graph engines, server DBs)

## Rationale

**Why SQLite:**
- Zero-config, single-file, no servers
- Battle-tested (billions of deployments)
- ACID transactions
- Recursive CTEs for graph traversal
- Cross-platform, permissive license
- Serves the "laptop scale" use case perfectly

**Why sqlite-vec:**
- Embedded vector search (no separate service)
- Lives in same file as assertions
- Acceptable performance for k=10 retrieval
- Pre-v1 but actively maintained (pin version, isolate behind port)

**Why pluggable:**
- Embedded-graph space is volatile (e.g. KùzuDB archived 2025)
- Deep traversal may need specialized backend
- Lets users scale out without changing code
- Storage backend is a clear extension point

## Consequences

**Positive:**
- ✅ Zero infrastructure barrier to adoption
- ✅ Users own their data (single file)
- ✅ Hybrid retrieval works out of the box
- ✅ Clean separation (port isolates dependency)

**Negative:**
- ⚠️ sqlite-vec is pre-v1 (must pin version, monitor)
- ⚠️ Recursive SQL for deep traversal (benchmark early, M1)
- ⚠️ Single-writer limitation (fine for proposals/review workflow)

**Mitigations:**
- sqlite-vec pinned + isolated behind `StorageBackend` port (swappable)
- Traversal benchmarked in M1, scale-out backend in M3
- Conformance kit lets any backend self-certify

## Alternatives Considered

**Embedded graph engines (KùzuDB, etc.):**
- Rejected: Volatile ecosystem, several projects archived
- Would need fallback anyway → just start with SQLite

**Server graph DB (Neo4j, etc.):**
- Rejected for default: Infrastructure barrier, not local-first
- Available as pluggable backend later

**Pure vector DB (Chroma, etc.):**
- Rejected: Loses symbolic querying, traversal, ACID

## Update (2026-09-09, closes KI-084): the single-writer limitation, made operational

This ADR's own Consequences named "single-writer limitation" as a known
negative and called it "fine for proposals/review workflow" — true of the
*logical* model (SPEC §10's routing is inherently sequential per subject),
but the sentence never said what happens at the SQLite level when a second
*process* (not thread — `ADR-0010`/KI-023's `threading.RLock` already
serializes this process's own threads) tries to write while one is still
mid-transaction, or what topology avoids it. Tracked as KI-084.

**The gap, concretely:** `SQLiteBackend.begin()` issued a plain deferred
`BEGIN`, which takes its read snapshot lazily, on first statement. Every
governed write path — all fourteen `with self.backend.transaction():`
call sites in `ontology.py` (`create_principal`, `assert_literal`,
`assert_ref`, `propose`, `propose_ref`, `retract`, `accept_proposal`,
`reject_proposal`, `request_changes`, `assign_reviewers`, `resubmit`,
`resolve_contradiction`, `flag_contradiction`, `apply_schema`) — reads
existing state before writing, all inside that one `transaction()` block —
so a concurrent writer in a second process could commit between that read
and this connection's own write. The write then
hit `SQLITE_BUSY_SNAPSHOT`, a stale-snapshot-upgrade failure SQLite
deliberately never routes through the busy handler: it failed immediately,
regardless of `busy_timeout`. Confirmed directly (two `SQLiteBackend`
instances on the same file, standing in for two OS processes, since
`self._lock` is per-instance and does not itself serialize them) before
this fix landed — see `tests/unit/test_sqlite_backend.py::TestConcurrency::
test_begin_blocks_on_cross_process_writer_then_reports_retryable` and
`test_begin_retries_and_succeeds_once_cross_process_writer_releases`.

**Fix:** `begin()` now issues `BEGIN IMMEDIATE`, which claims the write
lock at `begin()` time rather than lazily — so a concurrent writer is
serialized behind it the ordinary way, blocked and retried by SQLite's busy
handler (bounded by `busy_timeout`, now pinned explicitly to 5.0s in
`SQLiteBackend.__init__` rather than left as Python's implicit default) for
up to that window before genuinely failing, the same as any other reachable
`SQLITE_BUSY`. A failure in that window is now reported as a `StorageError`
whose message is distinguishable from a genuine storage fault — "lock
contention... safe to retry" — by masking the extended sqlite error code
(`code & 0xFF == sqlite3.SQLITE_BUSY`, needed because Python's `sqlite3`
surfaces the *extended* code, e.g. 517 for `SQLITE_BUSY_SNAPSHOT`, not just
the primary 5). Redacted like every other `StorageError` at the REST/
GraphQL/MCP boundary (ADR-0022, KI-083's precedent), so this
is a server-log-only improvement for API callers — the point is an operator
reading logs can now tell "retry the whole operation" from "something is
actually broken" without decoding the raw sqlite3 message.

**Deployment implication (what "single-writer" actually means in
practice):** exactly one OS process should hold the write path against a
given SQLite file at a time. This is not enforced by a lock file or PID
check — it is a topology recommendation, matching how the reference
deployment already runs (one REST/GraphQL/MCP process per database file;
`check_same_thread=False` plus the RLock only cover that one process's own
worker threads, per ADR-0010). Running two independent server processes
against the same file is now *safe* for every write path that opens a
`transaction()` — which is every governed write in `Ontology` (the
fourteen call sites named above) — no more instant, unretryable failure —
but still *serializes*:
the second process's writes queue behind the first's for up to
`busy_timeout` before erroring, which is a throughput cliff under real
contention, not a correctness one. Scaling writes across processes is out
of scope for the SQLite default and belongs to a server-backed
`StorageBackend` instead (tracked separately, not a change to this
default) — this fix does not claim to make that topology performant, only
safe for the paths it covers.

One write path is not among those fourteen, and this fix does not extend
to it: `Ontology.create_entity()` reads for `natural_key` uniqueness
(`_require_unique_natural_key`) and then writes, outside any
`transaction()` block — the same stale-read shape, just not one this KI's
fix reaches, since it never calls `begin()` at all (autocommit path). In
practice a race there surfaces as the `entity` table's own
`UNIQUE(namespace, concept, natural_key)` constraint failing loudly (a
`StorageError`, not a silent duplicate — KI-091's constraint already
covers the correctness half), so it's a narrower gap than the one this KI
closes, not a repeat of it — filed separately as KI-092 rather than folded
in here, since closing it means deciding whether `create_entity` should
gain its own `transaction()` wrapper, a scope question distinct from this
KI's "fix `begin()`'s SQL" mandate.

**A cost this fix introduces, not just documents:** `self._lock` (ADR-0010,
KI-023) is acquired *before* the `BEGIN IMMEDIATE` call, so while a
`begin()` here is genuinely contended by another process and parked in
SQLite's busy handler, every other call on *this* process — reads
included — blocks behind it for up to the full `busy_timeout`. Measured
directly: a same-process read blocked ~0.99s behind another thread's
`begin()` contending against a 1s `busy_timeout`. Before this fix, a
deferred `BEGIN` returned near-instantly and the eventual
`SQLITE_BUSY_SNAPSHOT` failure was also instant, so the lock was never
held this long — this is a genuinely new trade-off, not a pre-existing one
now merely written down. It cannot be avoided by acquiring the lock after
the `BEGIN IMMEDIATE` call instead: only one thread may safely issue a
statement on the single shared `self.conn` at a time regardless of which
statement it is, so narrowing the lock's span here would reopen KI-023
(two threads on one connection concurrently) rather than remove the
stall. In effect, one process's own read availability is now coupled to
how promptly a *different* process's writer releases the file lock — a
consideration for the single-writer-process topology recommended above,
not a reason to abandon it. A short-`busy_timeout` retry loop — retry
`BEGIN IMMEDIATE` in a bounded number of brief attempts, releasing
`self._lock` between them so other threads' reads can interleave — was
considered and rejected as disproportionate to this KI's scope: it adds
real complexity (retry/backoff logic, a new failure mode if all attempts
exhaust) to work around a stall that only manifests under genuine
cross-process write contention, the same condition the single-writer-
process topology recommendation above already exists to avoid.

## References

- PRD §8 P7 (Storage & persistence)
- PRD §16 Decision 1
- SPEC §12 (Storage layer)
- Implementation Plan §3.4 (Dependency hygiene)
- ADR-0010 (SQLite transaction model, `threading.RLock` / KI-023)
