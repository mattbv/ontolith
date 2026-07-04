# ADR-0010: SQLite Backend Transaction Model (Autocommit + Explicit Transaction Flag)

**Status**: Accepted  
**Date**: 2026-06-26  
**Deciders**: Ontolith Core Team  
**Related**: ADR-0001 (Storage Default), SPEC §9 (Proposal workflow)

---

## Context

The `SQLiteBackend` must support two distinct write patterns with conflicting requirements:

1. **Standalone writes** — individual operations (`put_entity`, `put_assertion`, `put_principal`, etc.) called directly from the SDK. These must be durable immediately: if the caller closes the connection after `put_entity()`, the entity must survive and be visible to a new connection.

2. **Batch transactions** — proposal acceptance (SPEC §9) requires multiple writes to be atomic. All writes land or none land, with `rollback()` undoing everything on failure. This is the "one transaction per proposal acceptance" invariant from the SPEC.

Python's `sqlite3` default mode ("legacy transaction mode") implicitly issues `BEGIN` before the first DML and holds the transaction open until an explicit `conn.commit()` is called. This means:

- Standalone writes are **not durable across connections** — data written in one connection is invisible to any other until `commit()` is called.
- The bug surfaces as "writes work within the same connection but disappear on `close()`," which is how all existing unit tests pass while the CLI (which opens a new connection per command) sees empty databases.

## Decision

Open the connection in **autocommit mode** (`isolation_level=None`) and track an explicit `_in_transaction: bool` flag on the backend instance:

```python
self.conn = sqlite3.connect(str(self.path), isolation_level=None)
self._in_transaction: bool = False
```

Each write method calls `conn.commit()` after successful DML **only when not inside an explicit transaction**:

```python
# After the INSERT/UPDATE succeeds:
if not self._in_transaction:
    self.conn.commit()
```

`begin()`, `commit()`, and `rollback()` manage the flag:

```python
def begin(self) -> None:
    self.conn.execute("BEGIN")
    self._in_transaction = True

def commit(self) -> None:
    self.conn.commit()
    self._in_transaction = False

def rollback(self) -> None:
    self.conn.rollback()
    self._in_transaction = False
```

## Rationale

**Why `isolation_level=None`:**
- Removes the implicit-transaction footgun from Python's `sqlite3` legacy mode
- SQLite's explicit `BEGIN`/`COMMIT`/`ROLLBACK` still work correctly in autocommit mode
- Standard approach for systems that need both per-operation durability and multi-statement transactions (matches SQLAlchemy's DBAPI layer behavior)

**Why a tracking flag instead of unconditional `conn.commit()` in every write method:**
- Unconditional auto-commit would break proposal acceptance: a `put_entity()` call inside `begin()`/`commit()` would commit prematurely, making `rollback()` unable to undo writes that already landed
- The flag makes the two modes explicit and testable

**Why not require callers to call `commit()` after every standalone write:**
- Leaks transaction management concerns into the application layer (Ontology, future MCP server, CLI)
- Error-prone: any caller that forgets would silently lose data

## Consequences

**Positive:**
- ✅ Standalone writes are immediately durable — cross-connection reads (CLI, second SDK instance) work correctly
- ✅ Batch transactions remain fully atomic — `rollback()` is safe and effective
- ✅ Future `StorageBackend` implementations have a clear protocol to follow: the same begin/commit/rollback contract applies to any backend

**Negative:**
- ⚠️ One extra `COMMIT` round-trip per standalone write — negligible for M1 write volumes
- ⚠️ `_in_transaction` is mutable state on the backend; callers must pair `begin()` / `commit()` correctly

**Implementation notes (M1):**
- A `transaction()` context manager (`with backend.transaction(): ...`) is provided on `StorageBackend` and `SQLiteBackend` to guarantee rollback on any exception and avoid wedged connections.
- The atomic multi-write path (proposal acceptance) is **not yet wired** in M1 — each `put_*` call at the `Ontology` layer commits independently. Wiring `begin()`/`commit()` around proposal acceptance is tracked for M2.

## Alternatives Considered

**Unconditional `conn.commit()` in every write method (no flag):**
- Rejected: Breaks atomic proposal acceptance — mid-transaction auto-commits make `rollback()` ineffective for writes already landed.

**Keep legacy transaction mode, require all callers to commit explicitly:**
- Rejected: Leaks transaction concerns upward; every SDK method, CLI command, and MCP handler would need to call `commit()` or risk data loss.

**Use SQLAlchemy for connection management:**
- Rejected for M1: Unnecessary dependency; adds complexity the `StorageBackend` port abstraction already handles.

## References

- ADR-0001: Storage Default (SQLite + sqlite-vec)
- SPEC §9: Proposal workflow and acceptance
- Python `sqlite3` docs: `isolation_level` parameter
