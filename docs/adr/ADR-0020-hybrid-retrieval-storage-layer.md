# ADR-0020: Hybrid Retrieval — Embedder Port, Vector Storage, and Query Layer

**Status**: Accepted
**Date**: 2026-07-20 (amended 2026-07-20 — query layer)
**Deciders**: Ontolith Core Team
**Related**: ADR-0001 (Storage Default), ADR-0004 (Confidence Semantics), ADR-0010 (SQLite
Transaction Model), ADR-0015 (Plugin Capability Isolation), ADR-0016 (DuckDB Second Backend),
SPEC §11.3 (Hybrid retrieval), SPEC §12.1 (Storage MUSTs), SPEC §12.3 (StorageBackend normative
shape), SPEC §14 (Embedder), Implementation Plan §2 (M3 scope), §9 (perf budgets), KI-018, KI-019

---

## Context

M3's scope table names "full hybrid retrieval" explicitly (KI-018). SPEC §11.3/§12.3/§14
normatively require an `Embedder` port, two new `StorageBackend` methods (`vector_upsert`/
`vector_search`), and a `QueryBuilder.semantic()` entry point. Before this ADR, none of this
existed: `sqlite-vec` was pinned as an optional dependency but never imported, `Embedder` had no
Protocol anywhere, and `QueryBuilder` had no vector-aware method.

This ADR originally covered the **storage layer** only — the `Embedder` port, its default
implementation, and the two new `StorageBackend` methods on both backends — deliberately scoped to
what was implemented in the first of two sequential PRs. The **Amendment** section below records
the query-layer decisions (`QueryBuilder.semantic()`, ranking algorithm, `.min_confidence()`/
`.trust_at_least()`, `Ontology.reindex()`) made in the follow-up PR that depends on it.

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

## Amendment (2026-07-20): Query layer — `semantic()`, ranking, filters, `reindex()`

The follow-up PR wires the storage layer above into `QueryBuilder` and `Ontology`.

### 5. Ranking algorithm: vector-search-first, symbolic-intersect (not "prefilter + rerank")

Implementation Plan §9's perf-budget row names the query "hybrid query (symbolic prefilter +
vector rerank), k=10" — the opposite order from what's implemented. `QueryBuilder.all()`, when
`.semantic(text)` is set, instead:

1. Embeds `text` via the configured `Embedder`.
2. Overfetches from the vector index: `vector_search("entity", query_vec, k_overfetch)` where
   `k_overfetch = min(max(limit or 20, 10 * (limit or 20)), 1000)`, ascending distance.
3. If `.where()` is also set, intersects the overfetched ids with `entities_where()`'s symbolic
   matches, preserving vector rank order — not the reverse (symbolic first, then rerank).
4. Applies `.min_confidence()`/`.trust_at_least()` as post-filters (§6 below), then `.limit()`.

Chosen over the Plan's literal wording because: (a) it needs zero non-normative storage additions
beyond `vector_search`'s SPEC-exact two-method shape; (b) bounded, predictable cost matters more
than exactness for a p95 budget; (c) `HashingEmbedder` is pure-Python — literally re-embedding and
re-ranking every symbolic candidate (the Plan's reading) would not stay within budget on a
realistic KB. The `Ontolith_Implementation_Plan.md` §9 wording should be read as superseded by this
ADR for the actual query order; a documentation follow-up should reconcile the phrasing.

**Consequence — recall-cutoff limitation:** a true symbolic match ranked below the overfetch window
in the *global* vector ranking is missed, even though it would satisfy `.where()`. This is a
deliberate v1 trade-off, not an oversight — see Consequences below.

### 6. `.min_confidence()`/`.trust_at_least()` — independent existential filters

An entity passes `.min_confidence(t)` iff it has **at least one** active assertion with
`confidence is not None and confidence >= t` (`None` never satisfies a numeric threshold, per
ADR-0004). An entity passes `.trust_at_least(l)` iff it has **at least one** active assertion
whose *effective* trust_level >= `l` — **amended by ADR-0033 (KI-047)**: for an assertion made
under delegation (`acting_as` set), this is `min(author.trust_level, acting_as.trust_level)`,
not simply the author's own `Principal.trust_level`; see ADR-0033 for the full formula and its
rationale. The two filters are independent of each other and
of `.where()`/`.semantic()` — nothing requires the *same* assertion to satisfy both, or requires
these filters to combine with `.semantic()` at all (they compose with the plain `.where()`/
`.entities()` path too). An entity has many assertions across many predicates; inventing a notion
of "the one assertion representing this entity" isn't supported elsewhere in the model, so an
existential (any-matching-assertion) reading is the only one consistent with how assertions already
work. Recency (`asserted_at`) is not used as an implicit tiebreaker beyond `.semantic()`'s own
distance-based order — no `.recent_first()` method is invented beyond what SPEC names.

### 7. No auto-embed-on-write; explicit `Ontology.reindex()`

`propose`/`accept_proposal` remain untouched — they have been through two audit-remediation arcs
and adding embedding side effects to the write path was out of scope for this decision. Instead,
`Ontology.reindex(concept: str | None = None) -> int` walks entities (optionally filtered by
concept), concatenates each entity's `Text`-typed active-assertion values into one string (stable
order: sorted by predicate, then `asserted_at`), embeds the batch, and upserts into
`scope="entity"`. Entities with no `Text`-typed active assertions are **skipped**, not
zero-vector-upserted — a zero vector would spuriously rank as "close" to other empty entities,
polluting `.semantic()` results. `reindex()` is idempotent (each call fully re-embeds and upserts)
and is exposed via `ontolith reindex [--concept]` in the CLI.

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
- **Recall-cutoff limitation (§5):** `.semantic()` combined with `.where()` can miss a true
  symbolic match if it ranks below the vector-search overfetch window globally. Acceptable for a
  bounded-cost v1; revisit if this proves user-visible (e.g. by widening the overfetch multiplier
  or, later, pushing `.where()` down into the vector search itself if the storage layer grows
  metadata-filtered search).
- `.semantic()` operates on current entity state only — it is not `as_of()`-aware in the way
  symbolic queries are (vectors represent "what `reindex()` last saw," not a bitemporal snapshot).
  `AsOfView.query(...).semantic(...)` is not rejected, but its results reflect the current vector
  index regardless of the `as_of` timestamp. Not fixed here: reconciling this needs the same
  schema-version-at-t machinery KI-019 already tracks as a separate gap.
- No write path auto-embeds; a caller who forgets to call `reindex()` after writing new Text
  assertions gets stale or empty `.semantic()` results with no error. This is deliberate (§7) but
  is a real footgun — worth a doc callout (README/quickstart) beyond this ADR.
- `reindex()` only upserts; it never purges. If an entity's Text-typed assertions are all later
  retracted/superseded, `_entity_text` correctly stops including it in the next `reindex()` call
  (§7's skip case), but its previously-upserted vector is not deleted — it stays in the index and
  can still surface in `.semantic()` results until a real `vector_delete`-style port method exists.
  No such method is added here; not exercised by any test.

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

**Symbolic-prefilter-then-rerank ranking (Implementation Plan §9's literal wording):** rejected —
see Amendment §5. Would require the query layer to re-embed and re-rank every symbolic match on
every call, which does not stay within the p95 budget under a pure-Python default `Embedder`, and
gains nothing the overfetch-and-intersect approach doesn't already provide at bounded cost.

**Requiring `.min_confidence()`/`.trust_at_least()` to be satisfied by the same assertion:**
rejected — see Amendment §6. Would require inventing a notion of "the entity's representative
assertion" that doesn't exist anywhere else in the model; every other query/filter operates over
the entity's full assertion set independently per predicate.

## References

- ADR-0001: Storage Default (SQLite + sqlite-vec)
- ADR-0004: Confidence Semantics
- ADR-0010: SQLite Backend Transaction Model
- ADR-0015: Plugin Capability Isolation
- ADR-0016: DuckDB Second Backend
- SPEC §11.3 (Hybrid retrieval), §12.1 (Storage MUSTs), §12.3 (StorageBackend), §14 (Embedder)
- Implementation Plan §2 (M3 scope), §9 (perf budgets)
- KI-018 (this ADR's resolution), KI-019 (related `as_of()` schema-version gap, §7 consequence)
