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
`BEGIN`, which takes its read snapshot lazily, on first statement.
`assert_literal`/`assert_ref` (SPEC §10 conflict routing), `propose`/
`propose_ref`'s auto-accept branch, `accept_proposal`, and `resubmit`'s
auto-accept branch (via `_replay_proposal_operations`) all do their
conflict-routing read *inside* the `with self.backend.transaction():`
block they open — so a concurrent writer in a second process could commit
between that read and this connection's own write. The write then
hit `SQLITE_BUSY_SNAPSHOT`, a stale-snapshot-upgrade failure SQLite
deliberately never routes through the busy handler: it failed immediately,
regardless of `busy_timeout`. Confirmed directly, ad hoc, against the
pre-fix code before this fix landed (two `SQLiteBackend` instances on the
same file, standing in for two OS processes, since `self._lock` is
per-instance and does not itself serialize them) — see KI-084's own
Description in `docs/known-issues.md` for that reproduction's detail. The
committed regression suite added alongside this fix — `tests/unit/
test_sqlite_backend.py::TestConcurrency::
test_begin_blocks_on_cross_process_writer_then_reports_retryable` and
`test_begin_retries_and_succeeds_once_cross_process_writer_releases` —
exercises the *post-fix* behavior (contention now blocks and retries
rather than failing instantly); reverting `BEGIN IMMEDIATE` alone doesn't
reproduce the original `SQLITE_BUSY_SNAPSHOT` failure either, since no
write is attempted after a reverted `begin()` returns — it instead surfaces
as `DID NOT RAISE`, a different, and equally valid, regression signal for
this fix.

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
GraphQL/MCP boundary — GraphQL and MCP via their own `_REDACT_MESSAGE_FOR`
tuple, REST via its `_STATUS_BY_ERROR_TYPE`-derived `status >= 500` check
(KI-083's precedent for why all three redact) — so this
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
against the same file no longer risks the instant, unretryable
`SQLITE_BUSY_SNAPSHOT` failure for the write paths named above — but this
is not a claim that every `Ontology` write is now cross-process-safe, and
two known, narrower gaps remain deliberately unfixed here (below). Even
where it does apply, safety still means *serializes*, not *parallelizes*:
the second process's writes queue behind the first's for up to
`busy_timeout` before erroring, which is a throughput cliff under real
contention, not a correctness one. Scaling writes across processes is out
of scope for the SQLite default and belongs to a server-backed
`StorageBackend` instead (tracked separately, not a change to this
default).

Two write shapes this fix does not reach, cross-referenced rather than
re-litigated:

- `create_entity`, `issue_token`, `revoke_token`, and `reindex` each read,
  then write, without ever calling `begin()` at all (autocommit) — `BEGIN
  IMMEDIATE` has nothing to change there. Of these, only `create_entity`'s
  read guards an invariant the write could violate (`natural_key`
  uniqueness); a race there still surfaces as a loud `StorageError` (KI-091's
  `UNIQUE` constraint), not a silent duplicate. Filed as KI-092, which also
  covers `propose`/`propose_ref`/`retract`'s reject/require-review outcome
  (below) as a related instance of the same "writes outside `transaction()`"
  shape.
- `propose`, `propose_ref`, and `retract` evaluate policy against a
  `kb_view` read taken *before* `begin()` is ever called, and persist a
  `Reject`/`RequireReview` decision (`_finalize_non_accepted_decision`) via
  that same pre-transaction, autocommit path when the outcome isn't
  auto-accept — this fix does not move that read or that write inside a
  transaction. This is not new: it is a pre-existing, deliberately accepted
  tradeoff recorded when KI-035 closed ("policy evaluation itself can
  likely stay outside the transaction... a pre-existing, deliberate
  tradeoff shared by `propose`/`propose_ref`/`retract`, not something newly
  closed here" — `resubmit`'s own KI-035 fix carries the identical caveat).
  KI-084 doesn't reopen that decision.

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
