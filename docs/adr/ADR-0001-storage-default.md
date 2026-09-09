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
write path (`assert_literal`, `assert_ref`, `propose`, `propose_ref`) reads
existing state for SPEC §10 conflict routing before writing, all inside one
`transaction()` block — so a concurrent writer in a second process could
commit between that read and this connection's own write. The write then
hit `SQLITE_BUSY_SNAPSHOT`, a stale-snapshot-upgrade failure SQLite
deliberately never routes through the busy handler: it failed immediately,
regardless of `busy_timeout`. Confirmed directly (two `SQLiteBackend`
instances on the same file, standing in for two OS processes, since
`self._lock` is per-instance and does not itself serialize them) before
this fix landed — see `tests/unit/test_sqlite_backend.py::TestConcurrency::
test_begin_blocks_on_cross_process_writer_then_reports_retryable` and its
`_releases` counterpart.

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
GraphQL/MCP boundary (ADR-0022 §error mapping, KI-083's precedent), so this
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
against the same file is now *safe* (no more instant, unretryable failure)
but still *serializes*: the second process's writes queue behind the
first's for up to `busy_timeout` before erroring, which is a throughput
cliff under real contention, not a correctness one. Scaling writes across
processes is out of scope for the SQLite default — it is exactly the case
this ADR's own "pluggable" decision exists for (a server-backed
`StorageBackend`, tracked separately, not a change to this default).

## References

- PRD §8 P7 (Storage & persistence)
- PRD §16 Decision 1
- SPEC §12 (Storage layer)
- Implementation Plan §3.4 (Dependency hygiene)
- ADR-0010 (SQLite transaction model, `threading.RLock` / KI-023)
