# ADR-0032: `DuckDBBackend` Serializes Connection Access With a `threading.RLock`, Mirroring `SQLiteBackend`'s KI-023 Fix

**Status**: Accepted
**Date**: 2026-08-24
**Deciders**: Ontolith Core Team
**Related**: ADR-0010 (SQLite transaction model — KI-023's update, the direct precedent this ADR mirrors), ADR-0016 (DuckDB second backend), KI-023, KI-046

---

## Context

`SQLiteBackend.begin()` acquires a `threading.RLock` (`self._lock`) and holds it across the full span of an explicit transaction (KI-023, recorded as an update to ADR-0010) — a losing thread in a race blocks in `begin()` until the winner commits or rolls back, then re-reads and provably sees the winner's committed state. `DuckDBBackend` had no equivalent: no lock, no guard of any kind around its single shared `duckdb.DuckDBPyConnection`. Two concurrent transitions on the same connection did not serialize the way SQLite's do.

This matters for the same reason KI-023 mattered for SQLite: the REST interface (ADR-0021/ADR-0022) dispatches sync route handlers onto an ASGI server's worker threadpool — a different OS thread than the one that constructed the backend — so genuinely concurrent access to a single `DuckDBBackend` instance is a real, not hypothetical, scenario once REST traffic is in play.

**Verified before deciding, not assumed:** DuckDB's Python driver reports `duckdb.threadsafety == 1`, the DB-API 2.0 level meaning "threads may share the module, but not connections" — i.e., a single `DuckDBPyConnection` object is not safe for concurrent use by multiple threads without external synchronization. This is the identical constraint `sqlite3` has (SQLite's own C library is more forgiving depending on compile-time threading mode, but the *stock Python `sqlite3` module wrapping it* still required `check_same_thread=False` to be explicitly lifted, and KI-023 is what happened once that check was lifted without a replacement synchronization mechanism). DuckDB has no equivalent same-thread check to lift in the first place — it simply provides no protection, silently, which is a *worse* default than sqlite3's (an unguarded same-thread violation there at least raises loudly).

Found during KI-035's review (2026-08-05) — flagged as a gap in the concurrency-serialization *guarantee* KI-035's proposal-transition TOCTOU fix implicitly relies on, since KI-035's own conformance vectors are parametrized over both backends via a mocked, single-threaded `_RacingClock` double, which can't surface a genuine cross-thread race either way.

## Decision

`DuckDBBackend` now holds a `threading.RLock` (`self._lock`), structurally identical to `SQLiteBackend`'s: `begin()` acquires it and holds it for the full span of an explicit transaction; every other public method (all 40 of them — the exact same set `SQLiteBackend` decorates, verified method-by-method) is wrapped with a `@_synchronized` decorator, byte-for-byte the same shape as `SQLiteBackend`'s own, that acquires the lock for the call's duration. `commit()`/`rollback()` release the lock with the same asymmetric pattern KI-023 established: `commit()` releases only on success (an unconditional release would double-release when `transaction()`'s `except` clause calls `rollback()` next after a failed `commit()`, which `RLock.release()` rejects with `RuntimeError`, masking the real `StorageError`); `rollback()` always releases, since it is the terminal cleanup path regardless of outcome.

**One deliberate divergence from `SQLiteBackend`: no `_in_transaction` flag.** `SQLiteBackend` needs one because it opens its connection in `isolation_level=None` (autocommit) mode and every write method checks `if not self._in_transaction: self.conn.commit()` to decide whether to auto-commit a standalone write (ADR-0010's own Decision). `DuckDBBackend` never had this pattern — DuckDB's own native autocommit already makes standalone writes durable without any Python-tracked flag (this module's own docstring already documented this divergence before this ADR: "Per-write methods therefore never call commit()/rollback() themselves"). The lock alone is sufficient to close KI-046; adding an unused flag would be exactly the kind of premature/unneeded abstraction this project's engineering conventions ask to avoid.

`begin()` and `rollback()` also gained the same `try`/`except duckdb.Error` wrapping into `StorageError` that `SQLiteBackend`'s already had — a smaller, adjacent inconsistency (DuckDB's `begin()` previously let a raw `duckdb.Error` escape uncaught) fixed alongside since this exact code was already being rewritten for the lock; not a separate KI, since it's the same "wrap backend errors" contract every other method in this file already follows.

## Rationale

**Why mirror `SQLiteBackend`'s exact mechanism rather than a different concurrency model:** the KI's own Fix text named two options — mirror the RLock, or "a different concurrency story (e.g. DuckDB's own multi-connection model, if `ontolith` ever moves away from one shared connection per backend instance)." The second option is explicitly conditional on an architecture change (per-thread cursors via `conn.cursor()`, each with its own transaction context) that hasn't happened and that no other part of this codebase's design points toward — `SQLiteBackend`, `DuckDBBackend`, and every caller (`Ontology`, `StorageBackend` port) all already assume one connection per backend instance. Introducing a materially different concurrency model for one backend, with no corresponding change to the other or to the port they both implement, would make the two backends behave differently under load in a way nothing in the conformance kit could catch (KI-046's own SPEC reference: "both backends must satisfy the same guarantees"). The RLock mirror keeps that guarantee true by construction, at the cost of the same tradeoff ADR-0010's KI-023 update already accepted for SQLite: no real write concurrency, since there was never any to lose with a single shared connection object either way.

**Why not a connection-pool / per-thread-cursor redesign instead:** rejected for the same reason ADR-0010's KI-023 update rejected it for SQLite — a valid *future* alternative if serialization becomes a measured bottleneck, not something to build speculatively now with no throughput data suggesting it's needed. Doing it for only one backend would also be the more novel, harder-to-maintain path for strictly worse cross-backend consistency in the meantime.

**Why the exact same method list, verified rather than assumed:** every public method `SQLiteBackend` wraps in `@_synchronized` has a same-named counterpart in `DuckDBBackend` (confirmed by diffing the two method name sets directly) — the two backends are structurally parallel by design (`ADR-0016`), so there was no reason to expect the synchronization surface to differ, and none was found.

## Consequences

**Positive:**
- Closes KI-046: `DuckDBBackend`'s single shared connection is now genuinely serialized across threads, matching `SQLiteBackend`'s guarantee — KI-035's proposal-transition TOCTOU fix (and any future fix relying on "the transaction serializes concurrent callers") is now airtight for both backends, not just SQLite.
- Real, threaded regression coverage: `tests/unit/test_duckdb_backend.py::TestConcurrency` mirrors `test_sqlite_backend.py::TestConcurrency`'s test shapes (a `ThreadPoolExecutor` + `threading.Barrier`/`threading.Event` forcing genuine concurrent contention, not mocked). Two of its four tests (`test_concurrent_transaction_blocks_survive_and_all_commit`, `test_entities_meeting_confidence_or_trust_serialized_with_open_transaction`) are confirmed, by reverting the fix and rerunning, to fail against the pre-fix code — independent proof the lock is load-bearing, not just structurally present.

**Negative / follow-ups:**
- **One test in the new suite (`test_concurrent_non_transactional_writes_are_serialized`) does NOT independently prove pre-fix risk** — confirmed by reverting the fix and rerunning it five times: it passes every time even without the lock. A single self-contained `execute()` call per thread, with no held-open explicit transaction, doesn't hit the interleaving window this fix actually closes (that requires a multi-statement `BEGIN...COMMIT` span). Kept for structural parity with `SQLiteBackend`'s own equivalent test and as forward-looking regression coverage for the `@_synchronized` decorator's presence on that method, but its docstring is explicit that it isn't independent proof the standalone-write path was ever unsafe pre-fix — don't cite it as such.
- Same tradeoff ADR-0010's KI-023 update already accepted for SQLite, now also true for DuckDB: this trades true write concurrency for correctness. `DuckDBBackend` is often chosen specifically for its OLAP/analytical performance characteristics; a reader evaluating DuckDB for *concurrent* throughput under this backend should know write-path concurrency is fully serialized, identically to SQLite, not a DuckDB-specific advantage this project's storage layer currently exposes.
- `begin()`'s and `rollback()`'s new `try`/`except duckdb.Error` branches are not independently regression-tested (matching `SQLiteBackend`'s own pre-existing, equally-untested `begin()` failure branch) — acceptable, consistent with existing precedent, not a new gap introduced disproportionately by this fix.

## Alternatives Considered

**Per-thread cursors via `conn.cursor()`, each with an independent transaction context (DuckDB's own documented multi-threading pattern):** Rejected for now — see Rationale. A valid future direction if a connection-pool redesign is ever pursued for either backend, but not a change to make for one backend in isolation.

**No lock; document DuckDB as single-threaded-only and rely on callers to serialize externally:** Rejected — `SQLiteBackend` already made the opposite call for the identical constraint (KI-023), and `StorageBackend`'s port contract makes no distinction between backends on this point; a caller (e.g. the REST interface) has no way to know which backend it's talking to and would need backend-specific serialization logic, defeating the point of the port abstraction.

## References

- ADR-0010 (SQLite Backend Transaction Model — the 2026-07-22 update recording KI-023's fix, this ADR's direct precedent and mechanism source)
- ADR-0016 (DuckDB Second Backend — establishes the structural-parallelism convention this ADR relies on)
- `src/ontolith/store/duckdb/backend.py` (`_synchronized`, `DuckDBBackend.begin`/`commit`/`rollback`/`__init__`)
- `src/ontolith/store/sqlite/backend.py` (`_synchronized` — the mirrored original)
- `tests/unit/test_duckdb_backend.py` (`TestConcurrency`)
- `tests/unit/test_sqlite_backend.py` (`TestConcurrency` — the mirrored original)
- `docs/known-issues.md` (KI-023, KI-035, KI-046)
- Python DB-API 2.0 (`threadsafety` attribute semantics)
