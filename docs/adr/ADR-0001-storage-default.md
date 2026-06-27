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

## References

- PRD §8 P7 (Storage & persistence)
- PRD §16 Decision 1
- SPEC §12 (Storage layer)
- Implementation Plan §3.4 (Dependency hygiene)
