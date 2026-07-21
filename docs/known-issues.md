# Known Issues and Gaps

Tracked gaps between the current implementation and the full SPEC/PRD requirements.
Each entry states the issue, its severity, the milestone it's targeted for, and where to find the relevant code.

---

## KI-001 — QueryBuilder.where() N+1 query pattern ✓ RESOLVED (M2)

**Severity:** Performance — critical gap against budget  
**Milestone target:** M2 — resolved in `perf(query): fix N+1 in QueryBuilder.where() with SQL subquery push-down`  
**SPEC reference:** SPEC §11, Implementation Plan §9 (p95 < 150 ms symbolic query)

### Description

`QueryBuilder.where()` in `src/ontolith/query/builder.py` filters by predicate value using Python-side iteration: it fetches all entities of the concept, then issues a separate `assertions()` SQL query per entity to check the predicate value. On the 100k-assertion benchmark seed (1k entities × 100 assertions) this produces 1,000 serial SQL round-trips, yielding a mean latency of ~15 seconds against the p95 budget of 150 ms — roughly 100× over budget.

M1 baseline captured in `tests/benchmarks/test_traversal.py::test_bench_symbolic_query_concept_filter`.

### Root cause

`builder.py:all()` calls `backend.entities()` for the concept scan, then for each entity calls `backend.assertions(subject=..., predicate=...)` and checks `value == filter_value` in Python. No SQL JOIN is used.

### Fix

Rewrite `QueryBuilder.all()` to issue a single SQL query joining `entity` and `assertion` on `subject`, filtering by `concept`, `predicate`, and `value_lit` in the WHERE clause. This will collapse the 1,000 round-trips into one indexed lookup.

---

## KI-002 — No governed retraction API on Ontology ✓ RESOLVED (M2)

**Severity:** Architecture gap — missing write surface  
**Milestone target:** M2 — resolved in `feat(govern): implement proposal workflow, conflict routing, and contradiction handling`  
**SPEC reference:** SPEC §9 (all writes through proposal path)

### Description

There is no `kb.retract()` or proposal-based retraction method on `Ontology`. The only way to retract an assertion in M1 is to call `kb.backend.set_assertion_status(id, "retracted")` directly — bypassing the proposal/policy path that SPEC §9 mandates for all writes.

This surfaces in the conformance vector `conformance/test_append_only.py::test_retracted_assertion_excluded_from_default_query`, which calls the backend directly.

### Fix

In M2, add `Ontology.retract(assertion_id, author)` that creates a `Proposal` with a retraction operation, runs it through `ThresholdPolicy`, and — if auto-accepted — calls `backend.set_assertion_status()`. This aligns retraction with the full governance pipeline.

---

## KI-003 — Append-only conformance vector does not directly test frozen model ✓ RESOLVED (M2)

**Severity:** Test gap — conformance coverage  
**Milestone target:** M2 — resolved in `test(conformance): add Hypothesis property tests for conflict routing and append-only invariants`  
**SPEC reference:** SPEC §5 (append-only assertions)

### Description

`conformance/test_append_only.py::test_value_field_is_immutable` demonstrates that new values create new assertion records rather than overwriting existing ones — but it never directly asserts that attempting to mutate `assertion.value` in-place raises an error.

### Fix

Added `test_assertion_value_mutation_raises` and `test_assertion_status_cannot_be_mutated_directly` to `conformance/test_append_only_properties.py`, directly asserting `pytest.raises(ValidationError)` on in-place field assignment.

---

## KI-004 — Symbolic query filter covers only `retracted` status in append-only vector ✓ RESOLVED (M2)

**Severity:** Test gap — partial conformance coverage  
**Milestone target:** M2 — resolved in `feat(govern): implement proposal workflow, conflict routing, and contradiction handling`  
**SPEC reference:** SPEC §5, §10

### Description

`conformance/test_append_only.py` tests that retracted assertions are excluded from default queries and retained in history. It did not cover the `superseded` and `flagged` statuses, which also must be excluded from default retrieval (SPEC §5) and queryable for audit.

### Fix

`conformance/test_conflict.py` now covers both: `status="flagged"` queryability (contradiction vectors) and `status="superseded"` queryability (supersession vectors).

---

## KI-005 — `assert_literal` / `assert_ref` bypass the proposal/policy path ✓ RESOLVED

**Severity:** Architecture gap — not SPEC-compliant for untrusted principals  
**Milestone target:** M2 added `propose()` as a governed path; `assert_literal`/`assert_ref`
themselves remained an ungoverned direct-write bypass until fixed on `fix/govern-write-pipeline`.  
**SPEC reference:** SPEC §9.3 ("A principal with `write` capability MAY bypass proposals; such
writes still pass through the conflict pipeline (§10) and still record full provenance.")

### Description

M2's `feat(govern): implement proposal workflow, conflict routing, and contradiction handling`
added `Ontology.propose(...)` as a governed path, but `Ontology.assert_literal()` and
`Ontology.assert_ref()` continued to call `backend.put_assertion()` directly with no
capability check and no conflict routing — any principal, including `ai`-kind ones, could
write an assertion that silently coexisted with a conflicting `static` fact with no
contradiction raised. This entry was previously marked resolved based on the *intended* fix
description below, but the code was never changed to match — the bypass was live until now.

### Fix

`assert_literal`/`assert_ref` now require `write` (or `admin`) capability, explicitly reject
`ai`-kind principals regardless of a misconfigured capability, and route through
`_apply_with_conflict_routing` (the same SPEC §10 routing `propose()`'s auto-accept path uses)
instead of writing directly. Per SPEC §9.3 they intentionally do **not** create a `Proposal`
record or evaluate `ThresholdPolicy` — that's the defined difference between the write-capable
bypass and the governed `propose()` path, not a gap to close.

---

## KI-006 — `govern/policy.py` `Reject` decision is never produced ✓ RESOLVED (M2)

**Severity:** Informational — dead code  
**Milestone target:** M2 — resolved in `feat(govern): wire Reject decision for read-only principals`  
**SPEC reference:** SPEC §9.2

### Description

`ThresholdPolicy.evaluate()` can return `AutoAccept` or `RequireReview` but never `Reject`. The `Reject` decision class exists but has no production path. Line 45 in `govern/policy.py` (`return Reject(...)`) is unreachable from `ThresholdPolicy`, leaving it uncovered.

### Fix

`ThresholdPolicy` now rejects proposals from principals with `read` capability immediately (no review queue). `Ontology.propose()` and `Ontology.retract()` handle the `Reject` branch: persist the proposal as `state="rejected"` with `decided_at` set, return without writing any assertions. 8 conformance vectors added in `conformance/test_proposal_workflow.py::TestRejectDecision`.

---

## KI-007 — No API to accept or reject a `require_review` proposal ✓ RESOLVED (M2)

**Severity:** Architecture gap — pending proposals are unresolvable  
**Milestone target:** M2 — resolved in `feat(govern): implement accept_proposal() and reject_proposal() review-acceptance API`  
**SPEC reference:** SPEC §9 (proposal state machine)

### Description

`Ontology.propose()` and `Ontology.retract()` can produce proposals with state `require_review` (e.g. for AI principals), and those proposals are persisted. However there is no `accept_proposal()`, `reject_proposal()`, or review-approval method anywhere in `src/ontolith/`. A reviewer cannot advance the proposal through the state machine (`require_review → under_review → accepted/rejected`), so pending proposals accumulate with no resolution path.

`update_proposal_state()` exists on the `StorageBackend` port and `SQLiteBackend` but is currently dead code — it was added in anticipation of this workflow.

### Fix

Add `Ontology.accept_proposal(proposal_id, reviewer)` and `Ontology.reject_proposal(proposal_id, reviewer, reason)` that advance the proposal state and — on acceptance — replay the stored payload operations through `_apply_with_conflict_routing`. Wire `update_proposal_state` into that path.

---

## KI-008 — Multi-target supersession links only the first superseded assertion

**Severity:** Informational — intentional v1 limitation  
**Milestone target:** Backlog  
**SPEC reference:** SPEC §10.2 (supersession chain)

### Description

When multiple overlapping `time_varying` assertions are superseded at once (e.g. two concurrent employers both active when a new one arrives), `_apply_with_conflict_routing` sets `supersedes = result.targets[0]` on the incoming assertion — linking it to only one predecessor. The other superseded assertions have no successor pointer.

`Assertion.supersedes` is a single `str | None` field by model design (one-to-one chain, not a list). This means time-travel via the `supersedes` chain cannot recover all concurrent predecessors — only the first.

In practice this is rare because `time_varying` with truly concurrent open windows indicates a data-entry race, but it is a known fidelity gap in the provenance trail.

### Fix

In a future milestone, change `Assertion.supersedes` to `list[str]` (or add a separate `supersession_link` table) to support many-to-one successor relationships. Any schema migration must preserve existing single-link records.

---

## KI-009 — Contradiction resolution API was missing ✓ RESOLVED (M2)

**Severity:** Major — SPEC §19 SHOULD-vector gap  
**Milestone target:** M2 — resolved in `feat(govern): implement resolve_contradiction() for contradiction resolution`  
**SPEC reference:** SPEC §10.3, §19 ("contradiction creation + resolution")

### Description

`Contradiction` carries `resolved_by`/`resolved_at` fields and `.claude/rules/conflict.md` documents a `resolve(c, winner_id, by)` function, but no code path ever called it. Contradictions could be created (flagged, routed to review) but never resolved — `state` stayed `"open"` forever and losing assertions stayed `"flagged"` indefinitely.

### Fix

Added `Ontology.resolve_contradiction(contradiction_id, winner_assertion_id, resolver)`: validates resolver has `review`/`admin` capability, contradiction is open, and the winner is an actual member; then retracts all other members, reactivates the winner, and marks the contradiction `resolved` — all inside one transaction. Added `StorageBackend.get_contradiction()` and `resolve_contradiction()` to the port and SQLite adapter. 12 conformance vectors in `conformance/test_contradiction_resolution.py`. Not exposed via MCP — resolution is a reviewer-only action, out of scope for the ADR-0008 propose-only surface.

---

## KI-010 — LinkML-aligned YAML schema dialect and reference plugins ✓ RESOLVED (M3)

**Severity:** Informational — scoped M2 deliverable, now complete  
**Milestone target:** M3 — LinkML dialect resolved via ADR-0013; reference plugins resolved via the reference plugin implementations below  
**SPEC reference:** Implementation Plan §2 (M2 exit list), ADR-0007 (interop priority), SPEC §13 (plugin protocols)

### Description

The Implementation Plan's M2 deliverable list includes "LinkML-aligned YAML, first 3 reference plugins" alongside review workflow, bitemporal time-travel, conflict handling, MCP server, and trust/delegation — all of which are now complete. The YAML dialect and plugin work were explicitly deferred: they require their own design pass (schema dialect coverage, bidirectional codegen, and the shape of the `Importer`/`Exporter`/`Reasoner`/`Validator` protocol implementations) rather than being folded into a conformance-gap sweep.

### Fix

**LinkML YAML dialect: resolved (ADR-0013).**

**Reference plugins: resolved.** Three first-party plugins, one per storage posture, implement the real `Importer`/`Exporter`/`Validator` protocols (`plugins/ports.py`) against the ADR-0015 capability-scoped views, and are registered as genuine `importlib.metadata` entry points in `pyproject.toml` (not test doubles) — proving plugin discovery, manifest negotiation, and view-scoped reads/writes end to end with working code:

- `CsvImporter` (`plugins/reference/csv_importer.py`, entry point `csv-importer`) — `importer` kind, `storage="write"`. Creates entities/assertions from CSV rows through `WriteView`, deduplicating entities by `natural_key` within a single import run.
- `JsonExporter` (`plugins/reference/json_exporter.py`, entry point `json-exporter`) — `exporter` kind, hard-capped `read`. Serializes active assertions to JSON through `ReadOnlyView`.
- `RequiredFieldsValidator` (`plugins/reference/required_fields_validator.py`, entry point `required-fields-validator`) — `validator` kind, hard-capped `read`. Demonstrates the kind of domain rule a `Validator` plugin exists for: a configurable required-predicate-per-concept business rule the core schema doesn't itself enforce.

`Reasoner`/`Connector` reference implementations were not built — the Implementation Plan's exit criterion is "first 3 reference plugins," met by the three above, one per storage posture (write/read/read+cross-check).

---

## KI-011 — Confidence-based auto-accept for AI proposals: deliberately not implemented

**Severity:** Informational — design decision, not a defect  
**Milestone target:** N/A — will not be implemented in v1  
**SPEC reference:** SPEC §19 (informative "SHOULD include vectors" list), Appendix A (informative example)

### Description

SPEC §19's SHOULD-vector list mentions "policy auto-accept vs. review by confidence threshold," and the informative Appendix A worked example shows an AI agent's proposal with confidence 0.82 going to `under_review` against an implied 0.9 auto-accept threshold — suggesting confidence ≥ 0.9 would auto-accept for an AI principal.

This was considered and explicitly rejected for Ontolith's `ThresholdPolicy`. Both SPEC §19's vector list and Appendix A are non-normative (informative/SHOULD, not MUST). Letting an AI principal's self-reported confidence score grant `AutoAccept` would mean an AI proposal can be committed with zero human in the loop — which conflicts with the project's AI-safety posture (SPEC §8.3: agents MUST default to `propose` and MUST NOT be granted `write` implicitly; SPEC §8.1: mandatory accountable owner; ADR-0008: no direct MCP write tool). A model's own confidence number is not a trust signal strong enough to bypass human review.

### Resolution

`ThresholdPolicy` always returns `RequireReview` for `kind == "ai"` principals, regardless of confidence or trust level. Confidence is still captured and visible to reviewers — it's stored on both `Proposal.payload["operations"][0]["confidence"]` and the resulting `Assertion.confidence` field — but it is a provenance/triage signal only, never a policy input. No code changes were needed for this decision since that storage already existed; this entry documents the choice so it isn't re-litigated as a "missing feature" in a future gap audit.

---

## KI-012 — Property tests (Hypothesis) only covered bitemporal reconstruction ✓ RESOLVED (M2)

**Severity:** Test-strategy gap — conflict routing and append-only invariants are correctness-critical and warrant property coverage, not just example-based vectors  
**Milestone target:** M2 — resolved in `test(conformance): add Hypothesis property tests for conflict routing and append-only invariants`  
**SPEC reference:** SPEC §19

### Description

Only bitemporal reconstruction (`conformance/test_bitemporal.py`) had a Hypothesis property test. Conflict routing (SPEC §10) and the append-only invariant (SPEC §5) — both correctness-critical, branch-heavy logic — had only example-based conformance vectors, which don't exhaustively probe edge cases the way generated inputs do.

### Fix

Added `conformance/test_conflict_routing_properties.py` — 8 property tests directly exercising the pure `govern/conflict.route()` function: empty-existing always activates, determinism, static routing contradicts iff any value differs (with exact member-set verification), time-varying supersedes iff window overlaps AND value differs, `_windows_overlap` symmetry, half-open boundary behavior, identical-window overlap, and fully-open-window overlap.

Added `conformance/test_append_only_properties.py` — 4 tests: direct frozen-field mutation checks (closing KI-003) plus two property tests driving a real `Ontology` through randomized `propose()`/`retract()` sequences, verifying no assertion's `value` ever changes after creation and the total record count never decreases.

---

## KI-013 — MCP provenance and flag_contradiction couldn't resolve non-active assertions ✓ RESOLVED (M2)

**Severity:** Correctness bug — audit trail unreachable for its primary use case  
**Milestone target:** M2 — resolved in `fix(interfaces): fix status filter bug in MCP provenance and flag_contradiction`  
**SPEC reference:** ADR-0008 (MCP surface)

### Description

`ontolith.provenance` and `ontolith.flag_contradiction` both called `kb.backend.assertions()` with no `status` argument. `StorageBackend.assertions()` defaults `status` to `"active"`, so both tools silently returned "not found" for any retracted, superseded, or flagged assertion. This defeated the purpose of `ontolith.provenance` (audit trail lookups are exactly the case where the assertion is often no longer active) and made `ontolith.flag_contradiction` unable to find an already-flagged member when extending an open contradiction with a third conflicting assertion.

Found while closing an MCP test-coverage gap (`interfaces/mcp.py` was at 78%) — writing a coverage test for the "extend existing contradiction" branch immediately surfaced the bug.

### Fix

Both call sites now use `assertions(status=None)` to search across all statuses. 5 new tests cover the fix: provenance on a retracted assertion, flag_contradiction assertion-not-found (both sides), and extending an existing contradiction with a third assertion.

---

## KI-014 — No plugin capability isolation (deny-by-default network/fs/write, manifest enforcement) — PARTIALLY RESOLVED (M3)

**Severity:** Architecture gap — security-relevant, but not yet exploitable since no plugin loading mechanism exists to secure
**Milestone target:** M3 — storage-capability isolation resolved via ADR-0015; network/filesystem enforcement remains open, tracked alongside KI-010's closure
**SPEC reference:** SPEC §13 (plugin protocols and discovery), §17 (security model, plugin sandboxing); Implementation Plan `plugins/` module (registry, protocols, lifecycle, sandbox)

### Description

`src/ontolith/plugins/` contained only protocol stubs (`ports.py`, `Embedder`/`PolicyStrategy`) — no registry, no manifest schema, no entry-point discovery exercised end-to-end, and critically, no capability sandbox. SPEC §13.1 requires plugins to declare a `name`/`version`/`capabilities` manifest and SPEC §13.2 requires reasoner-derived assertions to enter through the proposal path (never bypass governance), but none of this was enforced anywhere because nothing loaded a plugin.

Surfaced during the 2026-07-08 post-remediation security re-audit: a hypothetical in-process plugin, once loading exists, would run fully trusted and could call `backend.put_assertion` directly (bypassing governance) or `Ontology.issue_token` (see the now-admin-gated fix, MED2 in the same remediation) to mint itself a high-capability MCP credential.

### Fix

**Storage-capability isolation: resolved (ADR-0015).** A plugin is registered as a `service`-kind `Principal` with a capped `default_capability`; the sandbox is a capability-scoped facade over `Ontology` (`ReadOnlyView`/`WriteView`, `src/ontolith/plugins/views.py`) rather than a capability-restricted `StorageBackend` wrapper — the original fix sketch here was superseded by that approach (see ADR-0015's Alternatives Considered for why: a `StorageBackend` wrapper would reimplement policy/conflict-routing/model-requirement checks a second time, and wouldn't naturally block `issue_token`/`apply_schema` since those aren't `StorageBackend` methods anyway). Admin-only methods and the direct-write bypass are structurally absent from the views, not runtime-checked. `PluginRegistry.register()` (`src/ontolith/plugins/registry.py`) requires `admin` capability and discovers plugins via `importlib.metadata.entry_points(group="ontolith.plugins")`.

**Still open:** network/filesystem enforcement — the manifest schema declares these fields, but nothing enforces them yet; closing this requires process/wasm isolation, explicitly phased to later work in the Implementation Plan. ADR-0015 also states explicitly that the storage-isolation fix is a governance-correctness boundary, not a security sandbox against a plugin author who deliberately writes code to defeat the convention (Python has no true encapsulation) — that gap closes only with process/wasm isolation too.

---

## KI-015 — `propose()`/`propose_ref()`/`retract()` capability floor — RESOLVED (design clarification, no code change)

**Severity:** Informational — design clarification, not a defect
**Milestone target:** N/A — resolved by clarifying intended behavior, no implementation needed
**SPEC reference:** SPEC §8.3 (capability lattice: `read < propose < write < review < admin`), SPEC §9.1 (proposal state machine — `reject` as a terminal state reached via policy evaluation)

### Description

`Ontology.propose()`/`propose_ref()`/`retract()` resolve the author principal and check AI-model requirements and delegation authorization, but never check `principal.default_capability` directly before proceeding to `ThresholdPolicy.evaluate()` — unlike `create_entity` and `flag_contradiction`, both of which explicitly raise `CapabilityError` for `read`-capability authors before doing anything.

Surfaced during the code review for ADR-0015 (plugin capability isolation): `create_entity` gained a `>= propose` capability gate as part of that work, which made the asymmetry concrete — a `read`-only plugin principal is blocked from `create_entity` but reaches `ThresholdPolicy.evaluate()` via `propose()`/`propose_ref()`/`retract()`. This looked like a missing gate.

### Resolution

Not a gap — the two code paths are different by necessity, not inconsistent in effect. `create_entity`/`flag_contradiction` have no proposal/policy machinery at all, so a hard pre-check is their only mechanism for blocking `read`-capability authors. `propose`/`propose_ref`/`retract` already have that machinery, and route every author — including `read` capability — through `ThresholdPolicy.evaluate()`, which returns a `Reject` decision for `read`-capability principals (per KI-006's resolution): a persisted `Proposal` with `state="rejected"` and `decided_at` set, but no assertion ever written. This is SPEC §9.1's modeled behavior (`reject` is a first-class terminal state reached via policy, not a pre-check short-circuit), is covered by 8 existing conformance tests (`conformance/test_proposal_workflow.py::TestRejectDecision`), and is the contract documented in `interfaces/mcp.py`'s `propose_tool` docstring. Both mechanisms end in the same place — no assertion written, no elevated access granted — via the only path available to each. Adding a hard `CapabilityError` pre-check to `propose`/`propose_ref`/`retract` would not close a gap; it would regress all 8 conformance tests and contradict the SPEC-modeled state machine and the documented MCP contract.

---

## KI-016 — No second StorageBackend implementation for conformance kit ✓ RESOLVED (M3)

**Severity:** Architecture gap
**Milestone target:** M3
**SPEC reference:** SPEC §12 (Storage layer), SPEC §19 (Conformance kit)

### Description

Only `SQLiteBackend` implemented `StorageBackend`; the conformance kit had never been exercised against a second backend, leaving the port abstraction (`core`/`schema`/`govern`/`query` never importing a concrete adapter) unproven in practice and the M3 exit criterion ("Backend conformance kit passes on a 2nd backend") unmet.

### Fix

Implemented `DuckDBBackend` (`src/ontolith/store/duckdb/`), recorded in ADR-0016. Added `conformance/conftest.py` with a `make_kb` fixture parametrizing every backend-touching conformance vector over both SQLite and DuckDB (357 tests total, up from ~180 single-backend vectors); `tests/unit/test_duckdb_backend.py` gives the new backend its own dedicated unit coverage (53 tests, exceeding `SQLiteBackend`'s own coverage percentage). Three real DuckDB-specific divergences were discovered and resolved along the way, each documented inline in `backend.py`: DuckDB's `REAL` type is 4-byte single-precision (unlike SQLite's always-8-byte `REAL`), requiring `DOUBLE` for the `confidence` column; `at` is a reserved word requiring quoting; and DuckDB (confirmed on 1.5.4) raises a false-positive constraint violation on `UPDATE ... RETURNING` against a row that's the target of an incoming foreign key from another table — worked around via a `SELECT` existence check plus a plain `UPDATE`, and by dropping the `assertion_event.assertion_id`/`proposal_event.proposal_id` FK declarations (referential integrity there is enforced at the application layer instead, matching `Ontology`'s own write discipline). DuckPGQ/graph-native traversal remains out of scope — no traversal method exists on `StorageBackend` yet for it to serve (tracked separately, see Implementation Plan §14 item 1).

---

## KI-017 — `PolicyStrategy.evaluate()` has no `kb: ReadOnlyView` parameter

**Severity:** Architecture gap — blocks one class of policy strategy, not a defect in what exists
**Milestone target:** Backlog (design work needed before this can be scheduled)
**SPEC reference:** SPEC §9.2 (Policy engine contract — `evaluate` signature, purity requirement), §14 (Plugin protocols)

### Description

SPEC §9.2/§14 specify `PolicyStrategy.evaluate(self, proposal, principal, kb: ReadOnlyView) -> Decision`. The actual `PolicyStrategy` Protocol (`src/ontolith/govern/policy.py`) has `evaluate(self, proposal, principal, acting_as=None)` — no `kb` parameter, plus an `acting_as` parameter (needed for SPEC §8.4 delegation) the SPEC signature doesn't name. Without `kb`, a policy strategy cannot inspect KB state — `SourceQuorum` (one of SPEC §9.2's four SHOULD-have built-in strategies, which needs to count corroborating sources) cannot be implemented.

Recorded during a whole-project audit (2026-07-14) alongside ADR-0018, which made `PolicyStrategy` actually injectable (`Ontology(backend, policy=...)`) — closing the "not pluggable at all" half of the gap while deliberately deferring this half.

### Why not fixed now

Adding `kb: ReadOnlyView` isn't blocked by the SPEC's purity requirement ("no writes, deterministic given inputs" permits reads), but SPEC §9.2 also requires evaluation to be "testable and replayable" — a live `ReadOnlyView` over an open, potentially-concurrently-mutated connection has no fixed state to be reproducible against. Resolving this needs a snapshot/consistency contract (e.g., an `as_of`-pinned view at proposal-creation time) that doesn't exist yet, and design work against a real `SourceQuorum`-shaped consumer rather than speculatively. See ADR-0018's Rationale and Alternatives Considered for the full analysis.

### Suggested fix

When a concrete KB-inspecting policy strategy is actually being built: design the snapshot contract `evaluate()`'s `kb` parameter needs to satisfy "replayable," add it to the `PolicyStrategy` Protocol, and update the three `self.policy.evaluate(...)` call sites in `src/ontolith/ontology.py` to pass it.

---

## KI-018 — Hybrid retrieval (vector search + `Embedder`) never implemented ✓ RESOLVED (M3)

**Severity:** Architecture gap — SPEC-normative surface entirely missing, not partially built
**Milestone target:** M3
**SPEC reference:** SPEC §11.3 (Hybrid retrieval), §12.2 (`StorageBackend.vector_upsert`/`vector_search`), §14 (`Embedder` Protocol)

### Description

SPEC §11.3 specifies semantic search over entity/assertion embeddings via an `Embedder` port and a vector store, defaulting to `sqlite-vec`; SPEC §12.2's normative `StorageBackend` shape includes `vector_upsert(scope, id, vec)` and `vector_search(scope, vec, k)`; SPEC §14 defines the `Embedder` Protocol itself. None of this existed: `StorageBackend` (`src/ontolith/store/base.py`) had no vector methods on either backend, `Embedder` had no concrete Protocol anywhere in the codebase (`plugins/ports.py` documented its own absence explicitly), and `QueryBuilder` had no `.semantic()` method. `sqlite-vec==0.1.1` was a pinned optional dependency in `pyproject.toml` but was never imported anywhere — it did not back any working code path.

Surfaced during a whole-project docs/known-issues refresh (2026-07-14); this was a pre-existing gap, not a regression — hybrid retrieval was never scheduled before M4 in the Implementation Plan, so its absence through M3 was expected. Recorded so it stopped being an implicit assumption and started being an explicit, trackable gap against the SPEC's normative surface.

### Fix

Implemented across two sequential PRs, per ADR-0020 (amended for the query-layer decisions below):

**Storage layer:** `Embedder` Protocol (`src/ontolith/core/embedder.py`) with `HashingEmbedder` (dependency-free sha256 feature-hashing default) and `LookupEmbedder` (deterministic test double). `StorageBackend.vector_upsert`/`vector_search` added to the port and both adapters — SQLite via `sqlite-vec`'s `vec0` virtual tables (with a rowid-indirection table working around `vec0`'s lack of TEXT-primary-key upsert/delete support), DuckDB via its native `list_distance()` scalar function. `sqlite-vec` moved from optional to a required dependency, matching SPEC §12.1's MUST.

**Query layer:** `QueryBuilder.semantic(text)` composes with `.where()` per SPEC §11.3's example — a vector-search-first, symbolic-intersect algorithm (overfetches from the vector index, then intersects with `.where()` matches preserving vector rank order; see ADR-0020 for why this diverges from the Implementation Plan's literal "symbolic prefilter + vector rerank" phrasing, and the resulting recall-cutoff limitation). `.min_confidence(threshold)`/`.trust_at_least(level)` add independent existential post-filters; `.limit(n)` caps results. `Ontology.reindex(concept=None)` is the explicit (non-auto) entry point that embeds entities' Text-typed assertion content into the vector index — no write path auto-embeds on write. A CLI `ontolith reindex [--concept]` command and an informational hybrid-query benchmark (`tests/benchmarks/test_hybrid_query.py`, p95 well under the 150ms budget at 1k entities) were added alongside.

---

## KI-019 — `as_of(t)` does not resolve the schema version effective at `t`

**Severity:** Correctness gap — bitemporal reconstruction is schema-version-blind
**Milestone target:** Backlog
**SPEC reference:** SPEC §19 (informative SHOULD-vector: "`as_of` reconstruction across a schema migration"), `.claude/rules/bitemporal.md` ("Schema is resolved to the `schema_version` effective at `t`")

### Description

Bitemporal reconstruction is documented (and, per SPEC §19, expected to be tested) to resolve the schema that was active at time `t`, not the current schema — otherwise `as_of()` can misreport a property's `temporality`/`cardinality` for a point in time before a schema migration changed them. In practice, every schema-consulting call in `Ontology` (`_resolve_temporality`, `_resolve_cardinality`, `_require_known_predicate`, and `AsOfView` itself) calls `self.backend.get_schema(self.namespace)` with no version argument, which always returns the latest schema version regardless of the `as_of` timestamp in play. `StorageBackend.get_schema()` does accept an explicit `version: int | None` and schema versions are never deleted (`put_schema`'s docstring: "required for time-travel"), so the storage layer already retains what's needed — but nothing resolves *which* version was effective at a given `t`, because `SchemaIR` itself carries no timestamp (no `created_at`/`effective_at` field, and the schema table persists none either). This is a real gap, not yet exercised by any conformance vector, and only observable once a namespace has more than one schema version and an `as_of()` call spans the migration boundary.

Surfaced during a whole-project docs/known-issues refresh (2026-07-14).

### Fix

Add an effective-timestamp to schema persistence (either a new `SchemaIR` field populated via the injected `Clock` at `apply_schema()` time, or a separate table column not exposed on the IR itself), then add a `get_schema_at(namespace, t)` (or equivalent) resolution path that `AsOfView` and the temporality/cardinality/predicate-validation helpers use when operating under `as_of()`, instead of always resolving latest. Needs a conformance vector matching SPEC §19's own suggested case: a schema migration that changes a property's `temporality` or `cardinality`, with `as_of()` calls on both sides of the migration boundary asserting the pre-migration semantics still apply to pre-migration reconstruction.

---

## KI-020 — No automated `griffe diff` CI gate for public API breaking changes

**Severity:** Test gap — breaking changes to the public API surface are caught by convention/review, not CI
**Milestone target:** Backlog (bundle with the `security.yml`/`nightly.yml`/`release.yml` CI-hardening pass)
**SPEC reference:** Implementation Plan §5 (quality gates table — `griffe diff`, "warn pre-1.0, block post-1.0"), §7.1 (CI pipeline description)

### Description

ADR-0019 defines the public API surface and adds a pinned-`__all__` regression test (`tests/unit/test_public_api_surface.py`) as the M3-level starting point for "public API stability policy begins," but the Implementation Plan's quality-gates table separately names a `griffe diff` CI gate that would catch signature-level breakage (a parameter added/removed/retyped on an already-exported symbol) that the pinned-export-list test cannot. Investigated during ADR-0019: the installed `griffe` (via `griffelib` 2.1.0, pulled in transitively by `mkdocstrings[python]`) has its CLI split into a separate `griffecli` distribution not currently a project dependency — `python -m griffe` fails with `ModuleNotFoundError: griffecli`.

More fundamentally, this gate is one piece of a CI-hardening pass (alongside `bandit`, `gitleaks`, `pip-audit`, SBOM generation) described in Implementation Plan §7.1's `security.yml`/`nightly.yml`/`release.yml` workflows, none of which exist yet (only `ci.yml` does). Standing up `griffe` diffing in isolation, ahead of that surrounding pipeline, risks committing to tooling/config choices that should be made together.

### Fix

When the `security.yml` CI-hardening pass is built: add `griffecli` (or whatever the then-current griffe CLI package is) as a dev dependency, generate a baseline API snapshot (`griffe dump`), and wire a CI step that diffs the current surface against `main`'s baseline — informational/warn-only pre-1.0 per the Implementation Plan's own threshold, blocking only post-1.0.

---

## KI-021 — MCP read tools accept unauthenticated calls, contradicting SPEC §8.3

**Severity:** Architecture gap — SPEC compliance gap in a shipped interface, not yet exploitable beyond information disclosure (read-only)
**Milestone target:** Backlog (resolve alongside the REST interface work, KI-022, so both interfaces converge on one auth posture instead of being fixed twice)
**SPEC reference:** SPEC §8.3 ("`read`/`query`: required for any retrieval"), §17 ("every operation MUST be capability-checked ... against the resolved principal")

### Description

`src/ontolith/interfaces/mcp.py`'s `schema_tool`, `get_tool`, `query_tool`, and `provenance_tool` take no `token` parameter and call straight into `kb.backend`/`kb.query()` with no principal resolution at all — any MCP client can call them with zero credentials. Only `propose_tool` and `flag_contradiction_tool` require a bearer `token` (ADR-0014). SPEC §8.3 states plainly that `read`/`query` "required for any retrieval," and §17 states every operation MUST be capability-checked against a resolved principal — the four read tools currently satisfy neither.

Surfaced while scoping the REST interface (KI-022): REST's read routes are being designed to require auth from the start (matching SPEC §14.3's "auth required" language for REST resources), which would leave MCP and REST diverging on an identical class of operation for no principled reason — REST enforcing something SPEC already required of MCP too, and MCP simply never got it.

In practice this is a lower-severity gap than the CRITICAL/HIGH findings closed in the 2026-07-06/07 security remediation arc (see project memory) — it grants unauthenticated *reads*, not writes, capability escalation, or governance bypass. But it is a genuine, currently-live SPEC violation, not a documentation gap.

### Fix

Add a `token: str` parameter to `schema_tool`/`get_tool`/`query_tool`/`provenance_tool`, resolve it via the same `AuthProvider` already injected into `create_mcp_server`, and raise/return the same `auth_error` shape `propose_tool`/`flag_contradiction_tool` already use on failure. Since every capability level is `>= read` in the SPEC §8.3 ordering, this is purely "must resolve to *some* valid principal" — no new capability-tier logic needed, mirroring the read-auth design settled for REST in KI-022. Do this in the same pass as (or immediately after) the REST read-auth work so both interfaces are fixed from one shared understanding instead of two separate patches.

---

## KI-022 — REST interface (SPEC §14.3) not yet implemented

**Severity:** Architecture gap — named M3 scope item with zero implementation
**Milestone target:** M3 (first slice); full SPEC §14.3 parity is Backlog
**SPEC reference:** SPEC §14.3 (REST + GraphQL), §16 (error model), §17 (security model)

### Description

SPEC §14.3 defines a REST resource set — `/namespaces`, `/entities`, `/assertions`, `/proposals` (create + `/{id}/accept|reject|review`), `/contradictions/{id}/resolve`, `/principals`, `/query`, `/provenance/{assertion_id}` — spanning every capability tier (read, propose, write, review, admin). None of it exists: `src/ontolith/interfaces/` contains only `cli.py` and `mcp.py`. `pyproject.toml` already carries an unused `rest = ["fastapi>=0.110", "uvicorn>=0.27"]` optional-dependency group reserved for exactly this, dating back to M0's repo skeleton.

Scoped (design approved 2026-07-21) as two PRs rather than one, given the full resource set's span across capability tiers is a materially bigger surface than MCP's deliberately narrow read/propose-only tool set (ADR-0008):

- **First PR (planned next):** read + propose slice mirroring MCP's proven 6-tool surface — `GET /schema`, `GET /entities/{id}`, `POST /query`, `GET /provenance/{assertion_id}`, `POST /proposals`, `GET /proposals` — reusing ADR-0014's bearer-token `AuthProvider` unchanged, with auth required on every route including reads (see KI-021 for why that's the deliberate choice, and the resulting MCP inconsistency it surfaces). A new ADR records the auth-on-reads decision, the `OntolithError`→HTTP status mapping (SPEC §16), and the endpoint-to-MCP-tool mapping when this lands.
- **Deferred to a follow-up PR:** direct write (`/assertions` POST/PUT via `assert_literal`/`assert_ref`), `/proposals/{id}/accept|reject|review`, `/contradictions` (list + resolve) and `flag_contradiction`, `/principals` (create/list, token issue/revoke/list), `/namespaces`.
- **Deferred, separate concern:** offset/cursor-based pagination on `/query` — `QueryBuilder` itself only supports `.limit()` today, no `.offset()`; extending it is a `query/`+`store/` change, not an `interfaces/` one, and is out of scope for "expose the existing SDK over HTTP."

GraphQL (SPEC §14.3's other named half) is untouched by this KI and remains fully unscoped.

### Fix

Implement the first-PR slice above; update this entry to PARTIALLY RESOLVED (mirroring KI-014's pattern) noting what landed and what of the deferred list remains. Full resolution requires the follow-up PR closing the remaining bullet list, plus a decision on GraphQL's own scoping (separate KI or folded into this one once REST's shape is settled).

---

## Format

Each entry follows this structure:

```
## KI-NNN — Short title

**Severity:** [Blocking | Performance | Architecture gap | Test gap | Informational]
**Milestone target:** [M1 | M2 | M3 | M4 | Backlog]
**SPEC reference:** [section]

### Description
What the gap is and why it matters.

### Fix
What needs to be done.
```
