# ADR-0016: DuckDB as the Second StorageBackend

**Status**: Accepted
**Date**: 2026-07-13
**Deciders**: Ontolith Core Team
**Related**: ADR-0001 (Storage Default), ADR-0010 (SQLite Transaction Model), ADR-0011 (Provenance FK Enforcement), SPEC §12 (Storage layer), SPEC §19 (Conformance kit), Implementation Plan §14 item 1

---

## Context

The M3 exit criterion is "backend conformance kit passes on a 2nd backend." Only `SQLiteBackend`
implements `StorageBackend` today, so the port abstraction (`core`/`schema`/`govern`/`query` never
importing a concrete adapter) is asserted by design but has never been proven by a second,
independently-written implementation.

ADR-0001 already named DuckDB+DuckPGQ as the leading pluggable candidate for a scale-out backend
and explicitly rejected embedded graph engines as a *default* for ecosystem volatility (KùzuDB was
archived in 2025). Implementation Plan §14 item 1 left the final choice open pending a traversal
benchmark comparison. The team has now decided on DuckDB for the M3 conformance-kit second backend.

## Decision

Implement `DuckDBBackend` in `src/ontolith/store/duckdb/`, conforming to the same `StorageBackend`
Protocol as `SQLiteBackend`, and extend the conformance kit (`conformance/`) to run every applicable
vector against both backends.

**DuckPGQ / graph-native traversal is explicitly out of scope for this decision.** `StorageBackend`
exposes no traversal method today — only flat `assertions()`/`entities()`/`entities_where()`
filters — and `query/` has no traversal implementation to push a graph query down into. Adding
native graph traversal is a separate, future decision requiring its own port change and ADR.

## Rationale

**Why DuckDB over a graph engine:**
- ADR-0001 already rejected embedded graph engines for a *default* on volatility grounds; that
  concern applies equally to picking one as the conformance kit's proof backend.
- DuckDB is embedded and file-based — same zero-infrastructure operational profile as SQLite — so
  adopting it doesn't compromise the project's local-first stance the way a server backend would.
- `StorageBackend`'s current shape (CRUD + filtered row queries, no traversal) maps directly onto a
  relational engine; a graph engine's strengths wouldn't be exercised by the port as it exists today.

**Why not a server-based DB (e.g. Postgres):**
- Reintroduces an infrastructure dependency ADR-0001 deliberately avoided for the default. It
  remains a legitimate candidate for a future scale-out backend, but doesn't serve this decision's
  goal of proving the port abstraction with a second embedded engine.

**Why this is a meaningful proof of the abstraction, not a copy-paste exercise:**
- DuckDB's Python client has a different transaction/autocommit model, exception hierarchy, and row
  representation than `sqlite3` (no dict-like row objects; different constraint-violation exception
  types). A conformance-kit pass on both backends is a real test of `StorageBackend` as a boundary,
  not a trivial reskin.
- ID generation is already backend-agnostic (`Ontology` owns ULID generation via `IdProvider`), so
  no backend-specific ID scheme is needed — one less axis of divergence to design around.

**Why JSON fields are still hand-serialized (`json.dumps`/`json.loads`) rather than using DuckDB's
native `JSON` type:**
- Matches `SQLiteBackend`'s existing approach exactly. Each backend owns its own serialization; the
  port doesn't (and shouldn't) mandate a storage representation, only a Python-level contract.

## Consequences

**Positive:**
- Closes the M3 exit criterion with a genuinely independent backend implementation.
- Validates `StorageBackend` as a real abstraction boundary rather than an aspirational one.

**Negative / follow-ups:**
- DuckPGQ / graph-native traversal remains unimplemented; a future ADR and port change (a
  `traverse()`-style method) are needed if/when that's pursued.
- `Ontology.connect()` remains SQLite-only by design (ADR-0001's documented default). DuckDB access
  is via direct `Ontology(backend=DuckDBBackend(...))` construction — no `connect_duckdb()`
  convenience method is added, to avoid parallel public-API surfaces per backend before there's a
  clear need for one.

## Alternatives Considered

**Maintained embedded graph engine:** Rejected, consistent with ADR-0001's volatility concern in
that space, and because `StorageBackend` has no traversal method for a graph engine's strengths to
serve today.

**Server-based DB (Postgres):** Rejected for this decision — server-based, not local-first, and
doesn't serve the goal of proving the port on a second *embedded* engine. Remains a candidate for a
future scale-out-specific ADR.

**`connect_duckdb()` convenience classmethod on `Ontology`:** Rejected as scope creep for this pass
— direct construction (`Ontology(DuckDBBackend(path))`) is sufficient, and keeps `Ontology.connect()`'s
documented SQLite-default contract (ADR-0001) unambiguous.

## References

- ADR-0001: Storage Default (SQLite + sqlite-vec)
- ADR-0010: SQLite Backend Transaction Model
- ADR-0011: Provenance FK Enforcement
- SPEC §12 (Storage layer), SPEC §19 (Conformance kit)
- Implementation Plan §14 item 1
