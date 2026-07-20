# ADR-0020: Hybrid Retrieval — Embedder Port and Vector Storage

**Status**: Accepted
**Date**: 2026-07-20
**Deciders**: Ontolith Core Team
**Related**: ADR-0001 (Storage Default), ADR-0010 (SQLite Transaction Model), ADR-0015 (Plugin
Capability Isolation), ADR-0016 (DuckDB Second Backend), SPEC §11.3 (Hybrid retrieval), SPEC §12.1
(Storage MUSTs), SPEC §12.3 (StorageBackend normative shape), SPEC §14 (Embedder), Implementation
Plan §2 (M3 scope)

---

## Context

M3's scope table names "full hybrid retrieval" explicitly (KI-018). SPEC §11.3/§12.3/§14
normatively require an `Embedder` port, two new `StorageBackend` methods (`vector_upsert`/
`vector_search`), and a `QueryBuilder.semantic()` entry point. Before this ADR, none of this
existed: `sqlite-vec` was pinned as an optional dependency but never imported, `Embedder` had no
Protocol anywhere, and `QueryBuilder` had no vector-aware method.

This ADR covers the **storage layer** — the `Embedder` port, its default implementation, and the
two new `StorageBackend` methods on both backends. It is deliberately scoped to what is
implemented in this PR. The query layer (`QueryBuilder.semantic()`, ranking algorithm,
`Ontology.reindex()`) depends on this storage layer and lands in a follow-up PR; its
design decisions will be recorded as an amendment to this ADR once that PR is under review, rather
than speculatively decided here ahead of the code that implements them.

Two live-verified technical facts anchor the storage design:
- `sqlite-vec`'s `vec0` virtual table returns **L2 (Euclidean) distance** by default and does
  **not** support `DELETE`/`UPDATE`/`INSERT OR REPLACE` targeting a `TEXT PRIMARY KEY` column
  (confirmed empirically: raises `sqlite3.OperationalError: unknown error`). It **does** support
  `DELETE ... WHERE rowid = ?` on a rowid-only table (no declared TEXT primary key).
- DuckDB 1.5.4 has a **native** `list_distance(a, b)` scalar function (no extension needed) that
  returns bit-identical L2 distance to sqlite-vec for the same vectors, and supports
  `INSERT OR REPLACE` on a plain `TEXT PRIMARY KEY` table normally (confirmed empirically). This
  gives both backends the same distance metric with zero new DuckDB dependencies.

Given the `Embedder` contract (below) mandates L2-unit-normalized output vectors, ascending
L2-distance ranking is equivalent to descending cosine-similarity ranking
(`||u-v||² = 2 - 2·cos_sim(u,v)` for unit vectors) — one metric, no per-backend special-casing.

## Decision

### 1. `Embedder` lives in `src/ontolith/core/embedder.py`, not `plugins/ports.py`

SPEC's `Embedder` Protocol (`name`, `dim`, `embed(texts) -> vectors`) takes no `kb` parameter,
unlike every Protocol in `plugins/ports.py` (`Importer`/`Exporter`/`Reasoner`/`Validator`/
`Connector`), which exist specifically to receive a capability-scoped `ReadOnlyView`/`WriteView`
(ADR-0015). `Embedder` structurally can't participate in that pattern — it's a pure
`text -> vector` transform, the same shape as `Clock`/`IdProvider` (already in `core/`).
Implementation Plan §2 line 115 groups `Embedder` with the core-level ports (`StorageBackend`,
`Embedder`, `AuthProvider`, `PolicyStrategy`, `Clock`, `IdProvider`), not with the plugin
protocols. `plugins/registry.py`'s view-construction machinery confirms `PluginKind`'s previous
`"embedder"` literal was a phantom — nothing anywhere constructed a `kind="embedder"` plugin
through the capability-scoped registry. That literal is removed as a zero-call-site-impact
consistency fix.

A real ML-backed `Embedder` is just a class implementing the Protocol, passed into
`Ontology(embedder=...)` directly — the same pattern as a custom `PolicyStrategy` (ADR-0018), not
a plugin.

### 2. Default implementation: `HashingEmbedder`, dependency-free

No numpy/ML library exists anywhere in the project's dependency tree. The default embedder must
be pure-stdlib: lowercase + tokenize on `\w+`, hash each token via `hashlib.sha256` (not Python's
salted built-in `hash()`, which is non-deterministic across process restarts), map the digest to
a bucket index + sign, accumulate, L2-normalize. A zero-vector guard handles empty/whitespace
input without dividing by zero.

`LookupEmbedder` — a dict-backed deterministic test double, mirroring `FixedClock`/
`FixedIdProvider` naming — is also provided, since `HashingEmbedder`'s hash-bucket behavior isn't
hand-predictable and tests that need exact rank-order assertions need a controllable embedder.

### 3. Vector storage: closed scope set, lazy per-scope tables

`scope` is restricted to `VECTOR_SCOPES = frozenset({"entity", "assertion"})` (SPEC §11.3's own
language) rather than an arbitrary caller-supplied string used in dynamic DDL — an open string
would mean caller input drives table names (injection-shaped risk, unbounded schema growth). One
storage table per scope is created lazily on the first `vector_upsert` into that scope, once the
vector's dimension is known. A `vector_scope(scope TEXT PRIMARY KEY, dim INTEGER)` metadata table
(both backends) tracks the established dimension and causes later mismatched-dimension upserts or
searches to raise `ValidationError` — the safety net against silently corrupting search results if
an `Embedder` is swapped mid-KB-lifetime.

**SQLite**: because `vec0` rejects DELETE/UPDATE/INSERT-OR-REPLACE by TEXT primary key, the vec0
table (`vector_{scope}`) is kept **rowid-only** (`vec0(embedding FLOAT[N])`, no id column), and a
separate `vector_id_map(scope, id, vec_rowid)` table does id↔rowid translation. Upsert is
`INSERT` the new row, capture `lastrowid`, `DELETE ... WHERE rowid = ?` the old row if one
existed, then `INSERT OR REPLACE` the id-map entry — wrapped in an explicit `begin()`/`commit()`/
`rollback()` for atomicity, tracking whether the call opened its own transaction or joined a
caller's (so it neither double-BEGINs nor rolls back a transaction it doesn't own).

**DuckDB**: a plain `vector_{scope}(id TEXT PRIMARY KEY, embedding FLOAT[N])` table is used
directly — DuckDB's `INSERT OR REPLACE` works normally against a TEXT primary key, so no
rowid-indirection is needed. This matches every other write method in `DuckDBBackend`, which
relies on DuckDB's per-statement autocommit outside an explicit transaction and never calls
`commit()`/`rollback()` itself.

Search (`vector_search`) ranks ascending by L2 distance on both backends: SQLite via
`WHERE embedding MATCH ? ORDER BY distance LIMIT ?` (vec0's native query form), DuckDB via
`ORDER BY list_distance(embedding, ?) LIMIT ?`.

### 4. `sqlite-vec` moves from optional to required

SPEC §12.1 states the default SQLite backend MUST back embeddings with `sqlite-vec`. It moves
from `[project.optional-dependencies].vec` to the main `dependencies` list in `pyproject.toml`,
version-pinned (`sqlite-vec==0.1.1`) per the project's stated pre-v1 posture for this dependency.

## Rationale

**Why not extend `plugins/ports.py`'s `kb`-parameterized pattern to `Embedder`:** every existing
plugin kind exists to read from or write to the knowledge base through a capability-scoped view.
`Embedder` never touches the KB — it's a stateless (or externally-stateful, e.g. a loaded model)
function from text to vector. Forcing a `kb` parameter onto it would be a shape mismatch with no
behavioral benefit, and would require `PluginRegistry`'s view-construction logic to special-case a
protocol that structurally doesn't need a view.

**Why rowid-indirection only on SQLite, not a shared abstraction across both backends:** the two
backends have genuinely different upsert capabilities for their respective vector storage forms
(vec0's TEXT-PK restriction vs. DuckDB's normal TEXT-PK support). Forcing a shared indirection
layer onto DuckDB, which doesn't need one, would add complexity for no correctness gain — each
backend already owns its serialization/schema choices independently (ADR-0016 established this
precedent for JSON field handling).

**Why lazy per-scope tables instead of one fixed-width table for all vectors:** the port makes no
assumption about a single global embedding dimension — different `Embedder` implementations (or
future multi-model setups) may use different `dim` values per scope. Fixing dimension at
first-write time, tracked in `vector_scope`, defers that choice to runtime without requiring a
schema migration story for v1.

## Consequences

**Positive:**
- `Embedder` is testable in complete isolation from storage/KB machinery — `HashingEmbedder` and
  `LookupEmbedder` have zero I/O and are property-tested for determinism and unit-norm output.
- The two backends' vector methods are semantically identical from the `StorageBackend` port's
  perspective (same `ValidationError` conditions, same ascending-L2-distance contract), verified
  by one shared conformance suite (`conformance/test_vector_search.py`) parametrized over both.
- `sqlite-vec` becoming a hard dependency is now honest about the project's actual runtime
  requirements (SPEC already mandated it; the previous `optional` marking was aspirational, not
  load-bearing).

**Negative / follow-ups:**
- Install size and attack surface grow: `sqlite-vec` ships a native extension, the project's first
  non-pure-Python runtime dependency loaded via `enable_load_extension`. `enable_load_extension` is
  toggled back to `False` immediately after loading, and the load path is exercised by a dedicated
  unit test (`test_load_extension_disabled_after_init`) asserting it can't be re-enabled through
  the public connection object.
- `HashingEmbedder` is a deterministic placeholder, not a real semantic-quality embedder — nearest
  neighbors under feature hashing correlate with shared vocabulary, not meaning. This is
  acceptable for v1 (any real ML-backed `Embedder` is a drop-in replacement via the same
  Protocol) but should not be mistaken for production-quality retrieval.
- The query-layer decisions this storage layer enables (ranking algorithm, `min_confidence`/
  `trust_at_least` filter semantics, `reindex()` behavior) are intentionally deferred to a
  follow-up amendment of this ADR, made alongside the PR that implements them.

## Alternatives Considered

**`Embedder` as a `plugins/ports.py` protocol with a `kb` parameter:** rejected — see Rationale.
Would also have required `PluginRegistry`'s capability-view machinery to special-case a kind that
never uses the view it would be handed.

**A single shared vector table across all scopes, with a `scope` discriminator column:** rejected.
`vec0` virtual tables don't support arbitrary auxiliary/partition columns in the installed version
(0.1.1) — a discriminator column isn't expressible without abandoning `vec0` for SQLite entirely.
Per-scope tables sidestep this and keep both backends' schemas structurally parallel.

**Storing vectors as JSON-encoded text (matching how `Assertion.metadata` is serialized) instead
of native array columns:** rejected — neither backend's distance function operates over
JSON-encoded arrays; both `vec0 MATCH` and `list_distance` require the engine's native vector/array
column types.

## References

- ADR-0001: Storage Default (SQLite + sqlite-vec)
- ADR-0010: SQLite Backend Transaction Model
- ADR-0015: Plugin Capability Isolation
- ADR-0016: DuckDB Second Backend
- SPEC §11.3 (Hybrid retrieval), §12.1 (Storage MUSTs), §12.3 (StorageBackend), §14 (Embedder)
- Implementation Plan §2 (M3 scope)
