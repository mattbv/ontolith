# Architecture Decision Records (ADRs)

This directory contains Architecture Decision Records for Ontolith, documenting significant design decisions and their rationale.

## Format

We use the [MADR](https://adr.github.io/madr/) (Markdown Any Decision Records) format.

## Index

### Core Decisions (from PRD §16)

- [ADR-0001](ADR-0001-storage-default.md) — **Storage Default** (SQLite + sqlite-vec)
- [ADR-0002](ADR-0002-schema-definition.md) — **Schema Definition** (Class DSL + LinkML YAML via IR)
- [ADR-0003](ADR-0003-agent-identity.md) — **Agent Identity** (Service principal + accountable owner + per-assertion model)
- [ADR-0004](ADR-0004-confidence-semantics.md) — **Confidence Semantics** (Single scalar, no auto-combine in v1)
- [ADR-0005](ADR-0005-conflict-model.md) — **Conflict Model** (Routing by temporality: supersession vs contradiction)
- [ADR-0006](ADR-0006-licensing-and-business.md) — **Licensing & Business** (Open-core: Apache-2.0 + proprietary managed plane)
- [ADR-0007](ADR-0007-interop-priority.md) — **Interop Priority** (LinkML → RDF/OWL → agent-memory bridges)
- [ADR-0008](ADR-0008-mcp-surface.md) — **MCP Surface** (Read + propose, no direct write)

### Implementation Decisions (M1+)

- [ADR-0009](ADR-0009-trust-level-range.md) — **Trust Level Range** (0–10 integer with DB CHECK constraint)
- [ADR-0010](ADR-0010-sqlite-transaction-model.md) — **SQLite Transaction Model** (Autocommit + explicit transaction flag)
- [ADR-0011](ADR-0011-provenance-fk-enforcement.md) — **Provenance FK Enforcement** (DB-level FKs on `entity.created_by` and `assertion.author`)
- [ADR-0012](ADR-0012-class-dsl-compiler.md) — **Class DSL Compiler** (`compile_schema()`, explicit concepts, no global registry)
- [ADR-0013](ADR-0013-linkml-dialect-coverage.md) — **LinkML Dialect Coverage** (v1 scope, fail-loud on unsupported constructs)
- [ADR-0014](ADR-0014-mcp-authentication.md) — **MCP Authentication** (Per-principal API keys)
- [ADR-0015](ADR-0015-plugin-capability-isolation.md) — **Plugin Capability Isolation** (Service-principal-scoped views, storage capability enforced)
- [ADR-0016](ADR-0016-duckdb-second-backend.md) — **DuckDB Second Backend** (M3 conformance-kit second backend adapter, DuckPGQ/traversal deferred)
- [ADR-0017](ADR-0017-cardinality-conflict-semantics.md) — **Cardinality-Aware Conflict Routing** (many-cardinality static properties coexist instead of contradicting)
- [ADR-0018](ADR-0018-policy-strategy-injection.md) — **PolicyStrategy Injection** (Ontology accepts a custom policy; SPEC's kb parameter deferred pending a replay/snapshot design)
- [ADR-0019](ADR-0019-public-api-stability-policy.md) — **Public API Stability Policy** (`__all__`-defined surface, pinned-export regression test, `griffe` CI gate deferred)
- [ADR-0020](ADR-0020-hybrid-retrieval-storage-layer.md) — **Hybrid Retrieval** (`Embedder` port in `core/`, per-scope lazy vector tables, `sqlite-vec` required, `.semantic()` vector-search-first ranking, `Ontology.reindex()`)
- [ADR-0021](ADR-0021-rest-interface.md) — **REST Interface — Read + Propose Slice** (`create_rest_app()` factory, bearer-token auth on all routes, 6 MCP-mirrored endpoints + `GET /proposals`, centralized error mapping, typed Pydantic responses)
- [ADR-0022](ADR-0022-rest-write-review-admin.md) — **REST Interface — Write, Review, and Admin Slice** (direct write, proposal accept/reject, contradiction list/flag/resolve, principal creation + token admin; `Ontology.create_principal` AI-owner bug fixed; `/principals` list, `/namespaces`, `/proposals/{id}/review` deferred — no backing SDK method)

## Decision Process

1. **Propose** — Create ADR in draft status with context and options
2. **Discuss** — Review in team/community (via PR or RFC)
3. **Decide** — Update status to "Accepted" or "Rejected"
4. **Implement** — Reference ADR in implementation PRs
5. **Supersede** — If decision changes, create new ADR and mark old one superseded

## Status Values

- **Proposed** — Under discussion
- **Accepted** — Decision made and active
- **Rejected** — Decision considered but not adopted
- **Deprecated** — Previously accepted, now superseded
- **Superseded by ADR-XXXX** — Replaced by a newer decision

## Adding New ADRs

When making significant architectural decisions:

1. Copy the MADR template
2. Number sequentially (ADR-0009, ADR-0010, ...)
3. Use descriptive filename: `ADR-XXXX-short-title.md`
4. Fill in: Context, Decision, Rationale, Consequences, Alternatives
5. Reference related SPEC sections, PRD requirements, and other ADRs
6. Add to this index
7. Create PR for review

## Cross-References

- **PRD** — Product Requirements Document (`../Ontolith_PRD.md`)
- **SPEC** — Technical Specification (`../Ontolith_SPEC.md`)
- **Implementation Plan** — Engineering roadmap (`../Ontolith_Implementation_Plan.md`)
