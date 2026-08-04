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
- [ADR-0018](ADR-0018-policy-strategy-injection.md) — **PolicyStrategy Injection** (Ontology accepts a custom policy; SPEC's kb parameter deferred pending a replay/snapshot design — resolved by ADR-0025)
- [ADR-0019](ADR-0019-public-api-stability-policy.md) — **Public API Stability Policy** (`__all__`-defined surface, pinned-export regression test, `griffe` CI gate deferred)
- [ADR-0020](ADR-0020-hybrid-retrieval-storage-layer.md) — **Hybrid Retrieval** (`Embedder` port in `core/`, per-scope lazy vector tables, `sqlite-vec` required, `.semantic()` vector-search-first ranking, `Ontology.reindex()`)
- [ADR-0021](ADR-0021-rest-interface.md) — **REST Interface — Read + Propose Slice** (`create_rest_app()` factory, bearer-token auth on all routes, 6 MCP-mirrored endpoints + `GET /proposals`, centralized error mapping, typed Pydantic responses)
- [ADR-0022](ADR-0022-rest-write-review-admin.md) — **REST Interface — Write, Review, and Admin Slice** (direct write, proposal accept/reject/request_changes, contradiction list/flag/resolve, principal creation + token admin, `/principals` list, `/namespaces` list; `Ontology.create_principal` AI-owner bug fixed; namespace *creation* and multi-namespace scoping remain out of scope — project is still single-namespace throughout)
- [ADR-0023](ADR-0023-multi-target-supersession-audit-trail.md) — **Multi-Target Supersession Audit Trail** (`AssertionEvent.successor_id` recovers the full predecessor set an incoming assertion superseded, beyond `Assertion.supersedes`'s single-predecessor SPEC shape)
- [ADR-0024](ADR-0024-as-of-schema-resolution.md) — **Resolving the Schema Version Effective at `as_of(t)`** (new `StorageBackend.get_schema_at`/`AsOfView.schema()`, resolving the schema in force at a point in time rather than always the latest version)
- [ADR-0025](ADR-0025-policy-kb-parameter-and-source-quorum.md) — **PolicyStrategy `kb` Parameter and `SourceQuorum`** (`evaluate()` gains a required `kb: KbView` structural-Protocol parameter pinned to an `AsOfView` snapshot at proposal creation time; `SourceQuorum` built as the first KB-inspecting strategy)
- [ADR-0026](ADR-0026-security-ci-hardening.md) — **Security CI-Hardening Pass** (new `security.yml`: pip-audit/bandit/gitleaks/SBOM, PR + weekly; `griffe check` public-API diff gate in `ci.yml`, informational via `continue-on-error` — the tool itself exits 1 on any detected change; `mcp`/`sqlite-vec` bumped to fix 2 real CVEs; `nightly.yml`/`release.yml` remain out of scope)
- [ADR-0027](ADR-0027-defer-relation-traversal-and-lookup-operators.md) — **Defer Multi-Hop Relation Traversal and Lookup Operators in `QueryBuilder.where()`** (SPEC §11.1's `employer__name=`-style traversal and `__contains`-style lookup operators were never implemented and silently no-opped, KI-030; `.where()` now rejects any dunder filter key loudly instead, and SPEC/PRD/use-case docs were corrected to the equality-only contract the code actually supports)
- [ADR-0028](ADR-0028-value-type-enforcement-required-stays-plugin-level.md) — **`value_type` Enforced at Core Write Time; `required` Stays Plugin-Level** (KI-031: `assert_literal`/`propose` now reject a `value_type` mismatch against the schema's declared `PropertyDef.value_type`; `required` enforcement stays with the existing `RequiredFieldsValidator` plugin, a deliberate, recorded division of responsibility rather than a second core-layer check)

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
