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
- [ADR-0027](ADR-0027-defer-relation-traversal-and-lookup-operators.md) — **Defer Multi-Hop Relation Traversal and Lookup Operators in `QueryBuilder.where()`** (SPEC §11.1's `employer__name=`-style traversal and `__contains`-style lookup operators were never implemented and silently no-opped, KI-030; `.where()` now rejects any unsupported dunder filter key loudly instead. **Amended 2026-08-12**: lookup operators (`__contains`/`__gt`/`__lt`/`__gte`/`__lte`) were implemented, KI-039; multi-hop traversal remains deferred)
- [ADR-0028](ADR-0028-value-type-enforcement-required-deferred.md) — **`value_type` Enforced at Core Write Time; `required` Stays Out of Core** (KI-031, partial: `assert_literal`/`propose` now reject a `value_type` mismatch against the schema's declared `PropertyDef.value_type`; `required` enforcement is deliberately deferred — SPEC §4 assigns it to the validator layer and it's structurally the wrong shape for a per-write core gate. **Amended 2026-08-15**: the validator-layer gap this ADR left open (KI-041/KI-042) is now resolved — see ADR-0029. **Amended 2026-08-26**: KI-049 resolved — a literal's `value` content is now parsed against its declared `value_type`, not just the token; Boolean accepts case-insensitive `"true"`/`"false"` only, URI accepts LinkML's `uriorcurie` shape (full URI or CURIE); not retroactive against already-stored data or re-run at proposal replay, matching this ADR's own token-check precedent)
- [ADR-0029](ADR-0029-validator-invocation.md) — **Validator Invocation — Synchronous at Every Commit Point, with a Separate Completeness Path for `accept_proposal`** (KI-041/KI-042: `Ontology` gains `validators` (per-assertion, blocking, every commit point) and `completeness_validators` (whole-entity, `accept_proposal` only) constructor parameters; `RequiredFieldsValidator.from_schema()` derives its rule set from a schema's declared `required` fields)
- [ADR-0030](ADR-0030-retract-contradiction-capability-floor.md) — **`retract()` Routes to Review, Instead of Auto-Accepting, When It Can't Meet `resolve_contradiction()`'s Capability Floor** (KI-043: retracting a flagged contradiction member below `resolve_contradiction()`'s `review`/`admin` + non-AI floor now queues for review rather than raising outright, avoiding an inversion where a higher-capability principal would otherwise be worse off than a lower-capability one — **Breaking** behavior change, ordinary retraction unaffected. **Amended 2026-08-27**: KI-051 resolved — this floor and KI-033's party guard now apply to a contradiction member in *any* status, not just `flagged` (a `retracted`/`superseded` member of a still-open contradiction was previously exempt from both); `retract()` also gained a narrower, separate no-op for re-retracting an already-`retracted` target)
- [ADR-0031](ADR-0031-retracted-terminal-for-winner-selection.md) — **Retraction and Supersession Are Terminal for `resolve_contradiction()` Winner Selection Too** (KI-044: `resolve_contradiction()` now rejects a `retracted`/`superseded` winner candidate with `ValidationError` instead of reactivating it to `active` with a closed `valid_to`; also closes a related gap where extending an open contradiction could un-terminalize a `superseded` member — **Breaking** behavior change)
- [ADR-0032](ADR-0032-duckdb-concurrency-lock.md) — **`DuckDBBackend` Serializes Connection Access With a `threading.RLock`, Mirroring `SQLiteBackend`'s KI-023 Fix** (KI-046: `DuckDBBackend` had no equivalent of `SQLiteBackend`'s KI-023 concurrency lock — verified DuckDB's own DB-API `threadsafety` level requires the identical external synchronization; now decorates the same 40 public methods with `@_synchronized`, no `_in_transaction` flag needed since DuckDB's native autocommit already handles standalone-write durability)
- [ADR-0033](ADR-0033-trust-at-least-delegation-attenuation.md) — **`.trust_at_least()` Compares Effective (Delegation-Attenuated) Trust, Not the Author's Raw `trust_level`** (KI-047: `entities_meeting_trust` now joins the `acting_as` delegate and compares `min(author.trust_level, acting_as.trust_level)`, matching `govern/policy.py`'s existing formula — SQLite uses `min(a, b)`, DuckDB uses `least(a, b)` since DuckDB's `min` is aggregate-only for two scalar args; a dangling `acting_as` fails open (falls back to the author's own trust_level), a deliberate asymmetry with `_resolve_delegation`'s fail-closed behavior at write time — **Breaking** behavior change, non-delegated assertions unaffected)
- [ADR-0034](ADR-0034-cli-schema-migrate.md) — **`ontolith schema migrate` Is a Thin YAML-File Wrapper Around `apply_schema`; Data Migration Is Explicitly Out of Scope** (KI-048: reads a LinkML-aligned YAML document from disk and applies it as a new schema version via the existing governed `apply_schema` path — no new domain logic; class-DSL file input and any form of data backfill/reconciliation against already-stored assertions under a changed schema are explicitly deferred, not silently dropped)
- [ADR-0035](ADR-0035-flag-contradiction-eligible-winner.md) — **`flag_contradiction()` Requires at Least One Eligible Winner Among a *New* Contradiction's Founding Members** (KI-050: rejects opening a brand-new contradiction whose two founding members are both already `retracted`/`superseded`, mirroring `resolve_contradiction()`'s own winner-eligibility check (KI-044, ADR-0031) — extending an *already-open* contradiction with an all-terminal pair remains permitted, ADR-0031's own escape hatch is untouched — **Breaking** behavior change, no legitimate use case identified that this forecloses)
- [ADR-0036](ADR-0036-rdf-owl-bridge.md) — **RDF/OWL Bridge — `rdflib`-Backed, Schema + Active-Assertion Instance Data, Export Only** (M3: `schema/rdf.py::to_owl()` translates `SchemaIR` into an OWL ontology (concepts → `owl:Class`, properties → `owl:DatatypeProperty`, relations → `owl:ObjectProperty`) via a new `rdflib` dependency, not the real `linkml`/`linkml-runtime` packages ADR-0013 already rejected for the adjacent bridge; new `RdfExporter` reference plugin adds active-assertion instance data; deterministic `urn:ontolith:{namespace}:...` IRI scheme; export only, no `from_owl` import direction in v1)
- [ADR-0037](ADR-0037-graphql-interface.md) — **GraphQL Interface — `strawberry`-Backed, Query/Propose/Review Only, Narrower Than REST by Design** (M3, closes the last unstarted scope item: new `interfaces/graphql.py` exposes `Entity`/`Assertion`/`Proposal`/`Contradiction`/`Principal` types per SPEC §14.3's literal wording — no direct-write or principal-admin mutations, unlike REST's own extended surface (ADR-0022); bearer-token auth deferred to resolver-time via `context_getter` so introspection stays reachable unauthenticated; `strawberry.Schema.process_errors` centralizes `OntolithError` → `extensions={code, detail}` mapping, the GraphQL analog of REST's single exception handler; `EntityType.assertions` is a lazily-resolved nested field, not a flat REST-mirrored container; 2026-08-28 update converts every resolver to `async def` + `run_in_threadpool` offload, closing KI-052's event-loop-blocking gap)
- [ADR-0038](ADR-0038-admin-capability-gating.md) — **Close Two SPEC §17 Authorization Gaps in Principal/Admin Management** (M3 milestone-boundary security audit, KI-053/KI-054: `require_admin` now rejects AI-kind principals regardless of configured capability, mirroring `_require_reviewer_principal`'s existing pattern — closes a real privilege-escalation path where a misconfigured AI-admin could `issue_token()` for a human and impersonate them; CLI's `principal create` now requires `--author` naming an existing admin, mirroring REST's already-correct external-gate pattern, with a bootstrap exception for a database's first-ever principal — `Ontology.create_principal` itself stays intentionally ungated per ADR-0022, only the CLI needed to catch up)
- [ADR-0039](ADR-0039-retract-interface-exposure.md) — **`Ontology.retract()` Gains REST/GraphQL/CLI/MCP Routes, All at `propose` Tier** (KI-057: closes the last interface-exposure gap on the codebase's most heavily-governed write path — `POST /assertions/{id}/retract` (REST, `acting_as` as a query param), `Mutation.retract` (GraphQL), a new top-level `ontolith retract` CLI command (not nested under `assert`, which is a plain command, not a Typer group), and `ontolith.retract` (MCP, `propose` tier). MCP inclusion decided by tier, not by contradiction-adjacency: `retract()` is policy-evaluated and proposal-producing like `propose()`/`flag_contradiction()`, structurally unlike the reviewer-only, unconditionally-gated `resolve_contradiction()` that stays MCP-excluded)
- [ADR-0040](ADR-0040-composite-policy-strategy.md) — **`Composite` Policy Strategy — Severity-Ordered `all`/`any` Combination, No New "AI-Review" Strategy Shipped** (KI-061: builds SPEC §9.2's `Composite(all=…, any=…)`, the last of the six named `PolicyStrategy` implementations still missing — `all` uses the most-restrictive decision among its strategies, `any` the least-restrictive, same-severity decisions merged (reviewers dedup'd union, reasons concatenated) rather than one discarded. Closes the configuration gap ADR-0025 named but never built: `SourceQuorum`'s deliberate non-special-casing of AI authorship can now actually be paired with an AI-review rule via `Composite`, without reversing ADR-0025's own pinned decision or making the guarantee structural. No new "AI-always-reviews" strategy shipped — `ThresholdPolicy` can't be reused for it without re-imposing its own capability gate, and SPEC doesn't name one among its six strategies)

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
