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

## KI-008 — Multi-target supersession links only the first superseded assertion ✓ RESOLVED (M3)

**Severity:** Informational — intentional v1 limitation, now closed  
**Milestone target:** M3 — resolved via ADR-0023  
**SPEC reference:** SPEC §10.2 (supersession chain), §12.2 (normative SQLite schema)

### Description

When multiple overlapping `time_varying` assertions are superseded at once (e.g. two concurrent employers both active when a new one arrives), `_apply_with_conflict_routing` sets `supersedes = result.targets[0]` on the incoming assertion — linking it to only one predecessor. The other superseded assertions have no successor pointer.

`Assertion.supersedes` is a single `str | None` field by model design (one-to-one chain, not a list). This means time-travel via the `supersedes` chain cannot recover all concurrent predecessors — only the first.

In practice this is rare because `time_varying` with truly concurrent open windows indicates a data-entry race, but it is a known fidelity gap in the provenance trail.

### Fix

`Assertion.supersedes` stays a scalar `str | None`, unchanged — SPEC §12.2's normative schema models it as scalar `TEXT`, and widening it would break the public `Assertion` model (ADR-0019) to fix a rare-path fidelity gap. Instead, the full predecessor set is recovered through the existing `assertion_event` audit log, which already writes one row per superseded predecessor at exactly the right point in `_apply_with_conflict_routing`: `AssertionEvent` gained a `successor_id: str | None` field (populated only on `"superseded"` events), and `StorageBackend` gained `get_assertion_events_by_successor(successor_id) -> list[AssertionEvent]`. The full predecessor set for any successor is `{e.assertion_id for e in kb.backend.get_assertion_events_by_successor(successor_id)}`. Surfaced via `ProvenanceOut.superseded_ids` (REST) and the `superseded_ids` key on the MCP `ontolith.provenance` tool. See ADR-0023 for the full design, including why a new `supersession_link` table was rejected in favor of extending the audit log.

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

**Update (2026-07-27, KI-017/ADR-0025):** this resolution's "routed through `ThresholdPolicy.evaluate()`" framing was accurate when written, but ADR-0018 (not yet merged at the time) later made `PolicyStrategy` genuinely swappable — the capability floor above is enforced by `ThresholdPolicy`'s own logic, not by anything in `propose`/`propose_ref`/`retract` itself, so it is only as strong as whichever strategy is actually installed. `SourceQuorum` (ADR-0025) was found in review to omit this floor entirely — a `read`-capability principal's sufficiently-corroborated proposal would otherwise auto-accept — and now replicates `ThresholdPolicy`'s read-rejection explicitly. Any future `PolicyStrategy` (`ConfidenceThreshold`, `TrustLevel`, etc.) needs to make the same deliberate choice; it is not inherited for free.

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

## KI-017 — `PolicyStrategy.evaluate()` has no `kb: ReadOnlyView` parameter ✓ RESOLVED (M3)

**Severity:** Architecture gap — blocked one class of policy strategy; now closed
**Milestone target:** M3 — resolved via ADR-0025
**SPEC reference:** SPEC §9.2 (Policy engine contract — `evaluate` signature, purity requirement), §14 (Plugin protocols)

### Description

SPEC §9.2/§14 specify `PolicyStrategy.evaluate(self, proposal, principal, kb: ReadOnlyView) -> Decision`. The actual `PolicyStrategy` Protocol (`src/ontolith/govern/policy.py`) had `evaluate(self, proposal, principal, acting_as=None)` — no `kb` parameter, plus an `acting_as` parameter (needed for SPEC §8.4 delegation) the SPEC signature doesn't name. Without `kb`, a policy strategy could not inspect KB state — `SourceQuorum` (one of SPEC §9.2's SHOULD-have built-in strategies, which needs to count corroborating sources) could not be implemented.

Recorded during a whole-project audit (2026-07-14) alongside ADR-0018, which made `PolicyStrategy` actually injectable (`Ontology(backend, policy=...)`) — closing the "not pluggable at all" half of the gap while deliberately deferring this half.

### Why not fixed then

Adding `kb: ReadOnlyView` wasn't blocked by the SPEC's purity requirement ("no writes, deterministic given inputs" permits reads), but SPEC §9.2 also requires evaluation to be "testable and replayable" — a live `ReadOnlyView` over an open, potentially-concurrently-mutated connection has no fixed state to be reproducible against. Resolving this needed a snapshot/consistency contract that didn't exist yet, and design work against a real `SourceQuorum`-shaped consumer rather than speculatively. See ADR-0018's Rationale and Alternatives Considered for the original analysis.

### Fix

`PolicyStrategy.evaluate()` gained a required `kb: KbView` parameter (inserted between `principal` and `acting_as`, matching SPEC's ordering). `KbView` is a minimal structural Protocol declared locally in `govern/policy.py` (`assertions(subject=, predicate=) -> list[Assertion]`) rather than the SPEC-named `ReadOnlyView` — `ReadOnlyView` wraps a *live* `Ontology` and isn't the pinned type "testable and replayable" needs, and importing either concrete view class into `govern/policy.py` would also be circular. `Ontology.propose`/`propose_ref`/`retract` each pass `self.as_of(now)` (an `AsOfView` pinned at the proposal's own `created_at`) — since nothing is persisted until after the decision, this snapshot can't see the in-flight proposal's own operation *at evaluation time*. Replay is weaker than that: `asserted_at <= t` is inclusive, so re-running `as_of(proposal.created_at)` after anything else commits at that exact same instant *will* see it — replay reproduces the original read only absent a same-instant write afterward (real clocks: negligible; `FixedClock` in tests: not guaranteed). `ThresholdPolicy`'s own concrete `evaluate()` gives `kb` a default (`KbView | None = None`, unused) so none of the ~25 pre-existing pure `ThresholdPolicy` tests needed changes. `SourceQuorum` was implemented as the first KB-inspecting strategy — it also replicates `ThresholdPolicy`'s `read`-capability rejection, since nothing else gates it on the `propose`/`propose_ref`/`retract` paths (KI-015) — with conformance vectors in `conformance/test_source_quorum_policy.py` proving it reads real committed data through both backends. Full design in ADR-0025.

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

## KI-019 — `as_of(t)` does not resolve the schema version effective at `t` ✓ RESOLVED (M3)

**Severity:** Correctness gap — bitemporal reconstruction was schema-version-blind, now closed
**Milestone target:** M3 — resolved via ADR-0024
**SPEC reference:** SPEC §11.4 ("Schema is resolved to the `schema_version` effective at `t`"), §19 (informative SHOULD-vector: "`as_of` reconstruction across a schema migration")

### Description

Bitemporal reconstruction is documented (and, per SPEC §19, expected to be tested) to resolve the schema that was active at time `t`, not the current schema — otherwise `as_of()` can misreport a property's `temporality`/`cardinality` for a point in time before a schema migration changed them. `StorageBackend.get_schema()` only ever returned the latest schema version, with no way to ask for the version effective at a given `t`.

Surfaced during a whole-project docs/known-issues refresh (2026-07-14).

### Fix

Investigation while resolving this (see ADR-0024's Context) found the KI's original description overstated where the gap actually lived: `Ontology._resolve_temporality`/`_resolve_cardinality`/`_require_known_predicate` are write-time-only helpers (always correctly resolving the current schema, since writes happen "now"), and `AsOfView`/`QueryBuilder` never consulted schema at all — so there was no misreported data from any existing read path, only a missing capability. Also found that `schema_version.applied_at` already existed in both backends' storage, populated deterministically via each backend's injected `Clock` on every `put_schema` call — just never read back.

Closed additively: new `StorageBackend.get_schema_at(namespace, at) -> SchemaIR | None` port method resolves the highest version whose `applied_at <= at`, and new `AsOfView.schema()` calls it with the view's own `as_of` time. `SchemaIR` and `put_schema` are both unchanged — no breaking change. Conformance vectors in `conformance/test_bitemporal.py::TestAsOfSchema` cover the SPEC §19 suggested case (a property's temporality reconstructed differently on either side of a schema migration) on both backends.

---

## KI-020 — No automated `griffe diff` CI gate for public API breaking changes ✓ RESOLVED (M3)

**Severity:** Test gap — breaking changes to the public API surface were caught by convention/review, not CI; now closed
**Milestone target:** M3 — resolved via ADR-0026
**SPEC reference:** Implementation Plan §5 (quality gates table — `griffe diff`, "warn pre-1.0, block post-1.0"), §7.1 (CI pipeline description)

### Description

ADR-0019 defines the public API surface and adds a pinned-`__all__` regression test (`tests/unit/test_public_api_surface.py`) as the M3-level starting point for "public API stability policy begins," but the Implementation Plan's quality-gates table separately names a `griffe diff` CI gate that would catch signature-level breakage (a parameter added/removed/retyped on an already-exported symbol) that the pinned-export-list test cannot. Investigated during ADR-0019: the installed `griffe` (via `griffelib` 2.1.0, pulled in transitively by `mkdocstrings[python]`) has its CLI split into a separate `griffecli` distribution not currently a project dependency — `python -m griffe` fails with `ModuleNotFoundError: griffecli`.

More fundamentally, this gate was one piece of a CI-hardening pass (alongside `bandit`, `gitleaks`, `pip-audit`, SBOM generation) described in Implementation Plan §7.1's `security.yml`/`nightly.yml`/`release.yml` workflows, none of which existed yet (only `ci.yml` did). Standing up `griffe` diffing in isolation, ahead of that surrounding pipeline, would have risked committing to tooling/config choices that should be made together.

### Fix

New `.github/workflows/security.yml` (ADR-0026): `pip-audit`, `bandit`, `gitleaks`, CycloneDX SBOM generation — triggered on PR + weekly, matching Implementation Plan §7.1 exactly. Building it surfaced two real, previously-undetected issues that had to be fixed before the gates could actually be blocking: `pip-audit` found `mcp==1.28.0`/`sqlite-vec==0.1.1` had disclosed CVEs (both bumped to fixed versions), and `bandit` found 7 medium `B608` false-positive findings across two call-site shapes — 5 in the vector-search code (table names/an IN-clause arity string built from a validated closed set, not caller-controlled) and 2 in `entities_where()` (a `flagged_clause` local always one of two hardcoded literals) — each closed with a targeted `# nosec B608` and an explanatory comment rather than a blanket suppression. `griffecli` added as a dev dependency; a new `griffe check` step in `ci.yml`'s `quality` job diffs the public API against the PR's base commit and reports via native GitHub Actions annotations (`-f github`). This step is informational only because of `continue-on-error: true` on the CI step itself — `griffe check` exits `1` on any detected change, including non-breaking ones, not `0` as an earlier draft of this text claimed (a verification bug: piping the command through `head` before checking the exit code captured `head`'s exit code, not `griffe`'s; caught in review). `nightly.yml` (needs a benchmark-trend hosting decision) and `release.yml` (needs PyPI Trusted Publishing registration and a docs site, neither of which exist yet) remain deliberately out of scope — see ADR-0026 for why.

---

## KI-021 — MCP read tools accept unauthenticated calls, contradicting SPEC §8.3 ✓ RESOLVED

**Severity:** Architecture gap — SPEC compliance gap in a shipped interface, not yet exploitable beyond information disclosure (read-only)
**Milestone target:** Backlog (resolved alongside the REST interface work, KI-022, so both interfaces converge on one auth posture instead of being fixed twice)
**SPEC reference:** SPEC §8.3 ("`read`/`query`: required for any retrieval"), §17 ("every operation MUST be capability-checked ... against the resolved principal")

### Description

`src/ontolith/interfaces/mcp.py`'s `schema_tool`, `get_tool`, `query_tool`, and `provenance_tool` took no `token` parameter and called straight into `kb.backend`/`kb.query()` with no principal resolution at all — any MCP client could call them with zero credentials. Only `propose_tool` and `flag_contradiction_tool` required a bearer `token` (ADR-0014). SPEC §8.3 states plainly that `read`/`query` "required for any retrieval," and §17 states every operation MUST be capability-checked against a resolved principal — the four read tools satisfied neither.

Surfaced while scoping the REST interface (KI-022): REST's read routes were designed to require auth from the start (matching SPEC §14.3's "auth required" language for REST resources), which would have left MCP and REST diverging on an identical class of operation for no principled reason — REST enforcing something SPEC already required of MCP too, and MCP simply never got it.

In practice this was a lower-severity gap than the CRITICAL/HIGH findings closed in the 2026-07-06/07 security remediation arc (see project memory) — it granted unauthenticated *reads*, not writes, capability escalation, or governance bypass. But it was a genuine, live SPEC violation, not a documentation gap.

### Fix

Added a `token: str` parameter to `schema_tool`/`get_tool`/`query_tool`/`provenance_tool` (`src/ontolith/interfaces/mcp.py`), resolved via the same `AuthProvider` already injected into `create_mcp_server`, returning the same `{"error": ..., "code": "auth_error"}` shape `propose_tool`/`flag_contradiction_tool` already used on failure. Since every capability level is `>= read` in the SPEC §8.3 ordering, this was purely "must resolve to *some* valid principal" — no new capability-tier logic needed, mirroring the read-auth design settled for REST in ADR-0021. Documented as an update to ADR-0014. All 6 MCP tools now require a bearer token, closing the divergence with REST's read routes.

---

## KI-022 — REST interface (SPEC §14.3) ✓ RESOLVED (M3)

**Severity:** Architecture gap — named M3 scope item with zero implementation; all originally-deferred pieces now closed
**Milestone target:** M3 — full SPEC §14.3 parity (`/query` offset pagination, GraphQL) remains Backlog, tracked as separate concerns, not blocked on this KI
**SPEC reference:** SPEC §14.3 (REST + GraphQL), §16 (error model), §17 (security model)

### Description

SPEC §14.3 defines a REST resource set — `/namespaces`, `/entities`, `/assertions`, `/proposals` (create + `/{id}/accept|reject|review`), `/contradictions/{id}/resolve`, `/principals`, `/query`, `/provenance/{assertion_id}` — spanning every capability tier (read, propose, write, review, admin). None of it exists: `src/ontolith/interfaces/` contains only `cli.py` and `mcp.py`. `pyproject.toml` already carries an unused `rest = ["fastapi>=0.110", "uvicorn>=0.27"]` optional-dependency group reserved for exactly this, dating back to M0's repo skeleton.

Scoped (design approved 2026-07-21) as two PRs rather than one, given the full resource set's span across capability tiers is a materially bigger surface than MCP's deliberately narrow read/propose-only tool set (ADR-0008):

- **First PR:** read + propose slice mirroring MCP's proven 6-tool surface — `GET /schema`, `GET /entities/{id}`, `POST /query`, `GET /provenance/{assertion_id}`, `POST /proposals`, `GET /proposals` — reusing ADR-0014's bearer-token `AuthProvider` unchanged, with auth required on every route including reads (see KI-021, since resolved, for why that was the deliberate choice and the MCP inconsistency it surfaced at the time). ADR-0021 records the auth-on-reads decision, the `OntolithError`→HTTP status mapping (SPEC §16), and the endpoint-to-MCP-tool mapping.
- **Deferred to a follow-up PR:** direct write (`/assertions` POST/PUT via `assert_literal`/`assert_ref`), `/proposals/{id}/accept|reject|review`, `/contradictions` (list + resolve) and `flag_contradiction`, `/principals` (create/list, token issue/revoke/list), `/namespaces`.
- **Deferred again, in the follow-up PR itself:** `/principals` list and `/namespaces` — no backing SDK method existed for either (audited, not an oversight); `/principals` list has since been closed (see Fix).
- **Deferred, separate concern:** offset/cursor-based pagination on `/query` — `QueryBuilder` itself only supports `.limit()` today, no `.offset()`; extending it is a `query/`+`store/` change, not an `interfaces/` one, and is out of scope for "expose the existing SDK over HTTP."

GraphQL (SPEC §14.3's other named half) is untouched by this KI and remains fully unscoped.

### Fix

**Read + propose slice: resolved.** `src/ontolith/interfaces/rest.py` (`create_rest_app`, ADR-0021) implements `GET /schema`, `GET /entities/{id}`, `POST /query`, `GET /provenance/{id}`, `POST /proposals`, `GET /proposals` — all requiring an ADR-0014 bearer token, all errors mapped through one `OntolithError -> HTTP status` handler per SPEC §16.

**Write, review, and admin slice: resolved (ADR-0022, same day).** `POST /assertions` (direct write), `POST /proposals/{id}/accept|reject`, `GET /contradictions`, `POST /contradictions/flag`, `POST /contradictions/{id}/resolve`, `POST /principals`, and `/principals/{id}/tokens` (POST issue / GET list / DELETE revoke) — all reusing ADR-0021's auth and error-mapping machinery, eight of the ten routes needing no new capability-check code since the wrapped `Ontology` methods already gate themselves (the ninth, `GET /contradictions`, needs none either, but only because it's a read — see ADR-0022 §2). `POST /principals` is the exception: `Ontology.create_principal` has no built-in gate (matches the CLI's own ungated `principal create`), so the route calls `Ontology.require_admin()` explicitly. Building this slice also surfaced and fixed a real bug: `create_principal(kind="ai", owner=None)` raised a raw pydantic `ValidationError` instead of the documented `ontolith.core.errors.ValidationError`, which REST's error mapping couldn't catch — fixed in `Ontology.create_principal` itself (benefits every caller, not just REST). Review before merge found and closed four further issues (untyped `CreatePrincipalIn` fields letting other invalid inputs hit the same unmapped-pydantic-error class, a credential-id ordering ambiguity, a missing credential-ownership check on token revocation, and a capability-denial test-coverage gap) — see ADR-0022's Update section for the full list; one residual gap from that pass is tracked separately as KI-024.

**`GET /principals` (list): resolved (2026-07-26, ADR-0022 update).** New `StorageBackend.list_principals()` port method (both backends), `Ontology.list_principals(author)` (gated via the same `require_admin` used by `issue_token`/`revoke_token`/`list_tokens`), `GET /principals` (REST, admin-only), and `ontolith principal list` (CLI). No pagination, matching `list_tokens`'s existing precedent — see ADR-0022's Update section for why.

**`/proposals/{id}/review` (request_changes): resolved (2026-07-28, ADR-0022 update).** New `Ontology.request_changes(proposal_id, reviewer, reason="")` — SPEC §9.1's third `under_review` outcome (`changes_requested`), alongside the two `accept_proposal`/`reject_proposal` already implement. Reviewer-eligibility/proposal-state checks (previously duplicated between `accept_proposal`/`reject_proposal`) factored into a shared `Ontology._require_reviewer` helper. `ProposalEvent.type` widened to admit `"request_changes"`; the redundant DB `CHECK` constraint on both backends' `proposal_event.type` column was removed entirely rather than widened again (SPEC's own DDL has none, and widening it silently breaks the feature on any pre-existing database file — see ADR-0022's Update section for the full reasoning, including why this specific action keeps the same strict no-AI/no-self-review gate as accept rather than a lighter one). `POST /proposals/{proposal_id}/review` (REST) mirrors `/reject`'s shape exactly. The `require_review`/`under_review` state-naming conflation and resubmission (`changes_requested` → `submitted`) remain deliberately unaddressed.

**`GET /namespaces` (namespace registry): resolved (2026-07-28, ADR-0022 update).** New `Namespace` model (`ontolith.core.namespace`), `StorageBackend.list_namespaces()` port method (both backends, modeled on SPEC §12.2's own normative `namespace` table — resolving ADR-0022's own open question of table-vs-`DISTINCT` in the table's favor), `Ontology.list_namespaces()` (ungated, like `proposals()`/`contradictions()`), `GET /namespaces` (REST), and `ontolith namespace list` (CLI). Both backends idempotently register `DEFAULT_NAMESPACE` (`"default"`) at schema-creation time, and `put_schema()` idempotently registers `schema.namespace` too — otherwise a namespace with only a schema applied, no entities, would be invisible to the registry, the exact blind spot the table-over-`DISTINCT` decision was meant to close. No explicit `put_namespace`/create-namespace API was added; this project remains single-namespace throughout (ADR-0015). See ADR-0022's Update section for the full list of what's deliberately still out of scope (namespace-creation API, `Ontology.connect(namespace=...)`, per-namespace `principal_trust`/plugin isolation/read scoping, namespace-existence validation on other routes).

**Still open, tracked as separate concerns (not blocked on "no backing method"):** `/query` offset pagination — `QueryBuilder` only supports `.limit()`, no `.offset()`; extending it is a `query/`+`store/` change, out of scope for "expose the existing SDK over HTTP." GraphQL (SPEC §14.3's other named half) remains fully unscoped.

---

## KI-023 — SQLite backend's single connection is not safe under concurrent writes from an ASGI server ✓ RESOLVED

**Severity:** Architecture gap — data-integrity risk under real concurrent traffic, not yet triggered by any test (single-threaded today)
**Milestone target:** Backlog (resolved before REST, KI-022, was exposed to concurrent traffic)
**SPEC reference:** SPEC §12.1 (SQLite default backend, WAL mode), CLAUDE.md "One transaction per proposal acceptance"

### Description

`SQLiteBackend.__init__` (`src/ontolith/store/sqlite/backend.py`) opened its connection with `check_same_thread=False`, added when the REST interface (KI-022) was built: an ASGI server dispatches sync route handlers onto a worker threadpool, a different OS thread than the one that constructed the backend, which stock `sqlite3` blocks regardless of whether the cross-thread access is ever actually concurrent.

That flag only lifted the same-thread check — it did not serialize access. The connection's transaction state (`self._in_transaction`, `transaction()`'s `BEGIN`/`COMMIT` in `backend.py`) was shared, mutable, unguarded state. Two genuinely concurrent requests that both wrote (e.g. two overlapping `POST /proposals` that auto-accept) could interleave on the same connection: a second `BEGIN` while the first transaction was still open raised a raw `sqlite3.OperationalError` — `begin()` had no try/except around it, so this was not an `OntolithError` and was not caught by the REST error mapping (SPEC §16); it surfaced as FastAPI's generic, unmapped 500 — or worse, non-transactional autocommit statements from one request could interleave with another's open transaction, violating "one transaction per proposal acceptance" (CLAUDE.md) without necessarily raising anything.

Found during `ontolith-reviewer`'s pass on the REST interface's first PR (KI-022) — flagged MEDIUM there ("would elevate to HIGH if this REST surface is intended to serve concurrent traffic").

### Fix

Implemented option (a) from the original fix note — the correct general fix: `SQLiteBackend` now holds a `threading.RLock` (`self._lock`). `begin()` acquires it, held for the full span of an explicit transaction; every other public method is wrapped with a `@_synchronized` decorator that acquires the same lock for its own call. RLock's reentrancy lets a method called from inside an already-locked `transaction()` block (the common case — proposal acceptance) re-acquire on the same thread without blocking, while a different thread calling any method blocks until the lock is free.

`commit()`/`rollback()` release the lock asymmetrically, not both unconditionally: an `ontolith-reviewer` pass on this fix caught that unconditional release in both would double-release the lock when `commit()` fails inside a `transaction()` block (its `except` calls `rollback()` next, which would then release an already-released lock) — masking the real `StorageError` behind a `RuntimeError` and leaving `_in_transaction` stuck `True`. `commit()` now releases only on success; `rollback()` keeps the unconditional release, since it is always the terminal step regardless of outcome. Full details, the tradeoff analysis (serialization vs. real write concurrency, which this single-connection design never actually had), and the asymmetric-release rationale are recorded as an update to ADR-0010.

Regression coverage: `tests/unit/test_sqlite_backend.py::TestConcurrency` uses a `ThreadPoolExecutor` + `threading.Barrier` to force genuine concurrent contention on `begin()`/standalone writes — confirmed to reproduce the exact `sqlite3.OperationalError` described above against the pre-fix code, and to pass cleanly against the fix. A dedicated `test_commit_failure_inside_transaction_raises_storage_error_and_frees_lock` test forces a `commit()` failure inside a `transaction()` block and asserts a clean `StorageError` (not `RuntimeError`), a freed lock, and `_in_transaction is False` — confirmed to fail with exactly the predicted `RuntimeError` against the intermediate unconditional-release version found during review.

---

## KI-024 — `POST /principals/{id}/tokens` can return a mismatched `credential_id` under concurrent issuance ✓ RESOLVED

**Severity:** Architecture gap — narrow race, requires two admins issuing tokens for the same principal within the same request gap; not yet observed, no test reproduces it (the deterministic same-timestamp case it's adjacent to is fixed, see below)
**Milestone target:** Resolved via an update to ADR-0014
**SPEC reference:** ADR-0014 (credential lifecycle — each issued credential must be individually attributable and revocable)

### Description

`interfaces/rest.py`'s `issue_token_route` calls `Ontology.issue_token(principal_id, author)` (returns only the raw token — by design, it's never persisted) and then separately calls `Ontology.list_tokens(principal_id, author)[0].id` to recover the new credential's id, relying on `StorageBackend.get_credentials_for_principal`'s "most recently issued first" ordering. These are two independent, non-transactional calls.

Found during review of ADR-0022's write/admin REST slice: if a second admin issues another token for the *same* `principal_id` in the gap between this route's `issue_token()` and `list_tokens()` calls, `list_tokens()[0]` can return that second, unrelated credential instead of the one whose raw token was just handed back — pairing the correct raw token with the wrong `credential_id`. A caller who stores `(token, credential_id)` together and later calls `DELETE /principals/{id}/tokens/{credential_id}` would then revoke the wrong credential, silently leaving the intended (possibly compromised) one active.

A narrower, more likely variant — two credentials for the same principal sharing an identical `created_at` (coarse or injected `Clock`) — was fixed in the same PR by adding `id DESC` as a secondary sort key to `get_credentials_for_principal` (both backends), since ids are monotonically assigned and the newest one is always correct among same-timestamp ties. That fix does not close the cross-request race described here, which requires genuine concurrent issuance, not just a coarse clock.

### Fix

`Ontology.issue_token(principal_id, author) -> tuple[str, str]` now returns `(token, credential_id)` directly — the credential's id is already known at the point it's persisted, so neither caller needs a second, racy `list_tokens()[0]` lookup. Both `interfaces/rest.py`'s `issue_token_route` and the CLI's `principal issue-token` command were updated to unpack the tuple. Breaking change to `Ontology`'s public API (ADR-0019); recorded as an update to ADR-0014.

---

## KI-025 — CI's `mypy --strict` never type-checks `DuckDBBackend` against `StorageBackend` ✓ RESOLVED (M3)

**Severity:** Test gap — a future `StorageBackend` port addition could have landed SQLite-only and still passed every CI gate; now closed
**Milestone target:** M3 — resolved in `ci(quality): type-check conformance/conftest.py, resolve KI-025`
**SPEC reference:** Implementation Plan §2/§7.1 (dependency rule, quality gates), ADR-0016 (DuckDB backend)

### Description

CI's type-checking step is `uv run mypy --strict src` (`.github/workflows/ci.yml`) — `src/` only. `SQLiteBackend` gets checked structurally against the `StorageBackend` Protocol as a side effect: `Ontology.connect()` (`src/ontolith/ontology.py`) constructs one and passes it into `Ontology(backend: StorageBackend, ...)`, so a missing/mistyped Protocol method on `SQLiteBackend` would fail `mypy --strict src`.

`DuckDBBackend` has no equivalent path. The only place it's ever assigned to a `StorageBackend`-annotated slot is `conformance/conftest.py`'s `_duckdb_factory(...) -> StorageBackend` — and `conformance/` is never passed to `mypy` in CI. `uv run mypy --strict conformance/conftest.py` does pass today in isolation (confirmed), so the two backends currently agree — but nothing *guarantees* that going forward.

Surfaced during review of KI-022's `list_principals()` slice (PR #42): this is the second port-method addition in a row where both backends happened to get the method, checked only by discipline, not by CI. A future addition landing on `SQLiteBackend` alone (e.g. missed in a rushed PR, or added to `StorageBackend` and only one backend updated before the diff was reviewed) would still pass `ruff`, `mypy --strict src`, and the full test suite, since nothing structurally checks `DuckDBBackend`'s conformance to the Protocol in CI.

Widening CI's mypy invocation to the *whole* `conformance/` tree is not the cheap fix it first looks like: `uv run mypy --strict src conformance` (confirmed, run directly) currently fails with 28 pre-existing errors across `test_conflict.py`, `test_bitemporal.py`, `test_basic_assertion.py`, and `test_append_only_properties.py` — untyped/loosely-typed test fixtures and a couple of stale `# type: ignore` comments, unrelated to this KI. Only `conftest.py` alone is currently strict-clean.

### Fix

`.github/workflows/ci.yml`'s Type check step now runs `uv run mypy --strict src conformance/conftest.py` (was `src` only) — option (a) from the two considered. `DuckDBBackend`'s assignment to `_duckdb_factory(...) -> StorageBackend` is now structurally checked on every CI run, the same way `SQLiteBackend` already was via `Ontology.connect()`. A future port method missed on one backend now fails CI instead of passing silently.

The other 28 pre-existing mypy errors elsewhere in `conformance/` (untyped test fixtures, stale `# type: ignore` comments) are unrelated to this KI and remain out of scope — `conftest.py` was the only file that actually needed checking here, since it's the sole place a backend is assigned to a `StorageBackend`-typed slot outside `src/`.

---

## KI-026 — `resolve_contradiction()` has no self-resolution guard ✓ RESOLVED (M3)

**Severity:** Architecture gap — governance-integrity hole; a reviewer could unilaterally win a dispute they were party to, with zero check; now closed
**Milestone target:** M3 — resolved in `fix(govern): close resolve_contradiction self-resolution guard (KI-026)`
**SPEC reference:** SPEC §10.3 (contradiction resolution), ADR-0003 (self-review guard precedent)

### Description

`Ontology.resolve_contradiction(contradiction_id, winner_assertion_id, resolver)` (`src/ontolith/ontology.py`) checks that `resolver` has `review`/`admin` capability and is not AI-kind, but never checks whether `resolver` is the author (or delegate) of any member assertion in the contradiction — including the one being chosen as winner. `accept_proposal`/`reject_proposal`/`request_changes` all block exactly this via the shared `_require_reviewer` helper (`reviewer in (proposal.author, proposal.acting_as)`, ADR-0003's self-review guard); `resolve_contradiction` has no equivalent call anywhere in its body.

Concretely: a human principal with `review` capability who authored one of two disputed static facts can call `resolve_contradiction(contradiction_id, their_own_assertion_id, their_own_id)` and win — the other party's assertion is retracted, theirs reactivated, with no guard preventing it. `conformance/test_contradiction_resolution.py`'s existing vectors contain no self-authored-winner case.

`docs/adr/ADR-0022-rest-write-review-admin.md`'s §2 audit currently states this guard already exists for `resolve_contradiction`, grouping it with `accept_proposal`/`reject_proposal` — that claim is false for this method specifically.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

`resolve_contradiction` now resolves each member assertion (`self.backend.get_assertion(member_id)`) and raises `CapabilityError` if `resolver` is the author or delegate of *any* member — not just the winner, since an interested party shouldn't get to pick against their own losing entry either. All of this validation (state, membership, self-resolution) moved inside the existing `with self.backend.transaction():` block — a review pass caught that reading `contradiction.member_ids` before the transaction opened a real TOCTOU window (another thread could extend the same open contradiction via a concurrent `propose()` between the check and the write, and the new member would escape both the self-resolution check and the retraction loop). A member assertion that can't be resolved (`get_assertion` returns `None`) now raises `NotFoundError` rather than silently skipping the self-resolution check for it — assertions are append-only and never deleted, so a missing member is corruption, not a benign gap. Three new conformance vectors in `conformance/test_contradiction_resolution.py::TestResolveContradictionGuards` cover self-authored-winner, self-authored-losing-member, and delegate-authored cases. ADR-0022's §2 false claim corrected (it had also been copied into an already-written CHANGELOG entry — fixed there too).

Two related, narrower gaps were found while fixing this and are tracked separately rather than expanding this fix's scope: KI-033 (`retract()` lets a party to an open contradiction unilaterally retract the *opposing* member, achieving a similar outcome through a different method) and KI-034 (extending an open contradiction can resurrect an already-`retracted` member back to `flagged`).

---

## KI-027 — `request_changes()` produces a permanently stuck proposal, invisible to the default review queue ✓ RESOLVED (M3)

**Severity:** Architecture gap — a shipped governance action strands data with no recovery path
**Milestone target:** M3 — resolved in `feat(govern): add Ontology.resubmit(), close request_changes dead end (KI-027)`
**SPEC reference:** SPEC §9.1 (proposal state machine — `changes_requested ──resubmit──▶ submitted` is a normative transition, not terminal)

### Description

Once `Ontology.request_changes()` (ADR-0022 update, ships alongside `POST /proposals/{id}/review`) moves a proposal to `changes_requested`, nothing in the codebase can move it anywhere else — `_require_reviewer` (`src/ontolith/ontology.py:1125-1163`) only accepts proposals in `("require_review", "under_review")`, so any further `accept_proposal`/`reject_proposal`/`request_changes` call raises `ValidationError`. No `resubmit` method exists anywhere (confirmed via grep for `changes_requested` across `src/`). `conformance/test_review_workflow.py`'s dead-end test (`test_changes_requested_is_a_dead_end_for_further_review`) pins this as expected behavior, not a bug to fix — the gap was known at ship time (ADR-0022's Update section names it as deliberately out of scope) but was never logged as its own tracked issue, only as ADR prose.

Compounding this: `Ontology.proposals()` defaults to `state="require_review"` (`src/ontolith/ontology.py`) — the call a reviewer would naturally make to see "what's pending" — which silently excludes `changes_requested` proposals. A reviewer must already know to pass `state="changes_requested"` or `state=None` to ever see a proposal again after requesting changes on it.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

Added `Ontology.resubmit(proposal_id, author)` implementing SPEC §9.1's `changes_requested → submitted → {policy}` transition: only the proposal's own author or delegate may call it (the inverse of `_require_reviewer`'s self-review guard); the existing payload is replayed unedited through a fresh policy evaluation, but — unlike `propose`/`propose_ref` — the `kb_view` pinned for that evaluation is the *resubmission* instant, not `proposal.created_at` (ADR-0025 update): the proposal already exists, so a KB-reading `PolicyStrategy` must see what's true now. `Ontology.accept_proposal`'s operation-replay loop was extracted into a shared `_replay_proposal_operations` helper so `resubmit`'s auto-accept branch doesn't duplicate it. `_finalize_non_accepted_decision` (shared with `propose`/`propose_ref`/`retract`) gained an `is_new` flag: `resubmit` re-decides an *existing* persisted row via `update_proposal_state`, where the original callers `INSERT` a brand-new one via `put_proposal` — reusing the insert path on an existing id raised a UNIQUE-constraint `StorageError`, caught by the conformance suite before this shipped. Unlike `propose`'s own auto-accept path (which records no event for a brand-new proposal), `resubmit` always records a `ProposalEvent(type="resubmit")` regardless of outcome — it re-decides an already-persisted row, and a `require_review` outcome with no event would leave no trace of when policy last ran. REST gained `POST /proposals/{id}/resubmit`, MCP gained `ontolith.resubmit` (the only MCP-exposed way for an AI author to act on its own `changes_requested` proposal, since AI proposals always route to require_review per ADR-0003 and carry no write fallback); CLI parity (an explicit `ontolith proposal resubmit` command, vs. the SDK method already reachable through `ontolith proposal list --state pending` for discovery) is deferred to KI-032 (CLI proposal-review commands).

`Ontology.proposals()` gained a `state="pending"` query-level alias merging `require_review` and `changes_requested` — both are still-open proposals needing someone's attention, and without it a `changes_requested` proposal was invisible to any single-state query even after `resubmit` existed to act on it. `"pending"` is deliberately **not** the default: the canonical reviewer loop (`for p in kb.proposals(): kb.accept_proposal(p.id, ...)`) assumes every returned proposal is reviewer-actionable, which is only true of `require_review` — `changes_requested` proposals raise `ValidationError` from `accept_proposal`/`reject_proposal`. The default stays `state="require_review"`; pass `state="pending"` explicitly (`GET /proposals?state=pending`, `ontolith proposal list --state pending`) to see both.

---

## KI-028 — `.min_confidence()`/`.trust_at_least()` reintroduce an N+1 backend-round-trip pattern ✓ RESOLVED (M3)

**Severity:** Performance — unbounded per-entity (and per-assertion) backend round trips, unbenchmarked
**Milestone target:** M3 — resolved in `perf(query): push min_confidence/trust_at_least down to bulk backend lookups (KI-028)`
**SPEC reference:** Implementation Plan §9 (performance budgets)

### Description

`QueryBuilder._apply_confidence_trust_filters` (`src/ontolith/query/builder.py:216-235`) runs after `_base_candidates()` resolves the full pre-filter entity list — unbounded when neither `.where()` nor `.semantic()` is chained (`self._backend.entities(namespace=, concept=)` returns every entity of the concept). For each candidate, `_passes_confidence_trust` issues a separate `backend.assertions(subject=entity.id, status="active")` call, and `.trust_at_least()` additionally calls `backend.get_principal(author_id)` once per assertion, uncached even across repeated authors within the same query. `kb.query("Person").trust_at_least(5)` with no other filter on a 100k-entity concept issues on the order of 100k+ separate backend calls before `.limit()` is ever applied.

This is the same defect class KI-001 was created and fixed for — reintroduced, apparently unnoticed, when `.min_confidence()`/`.trust_at_least()` shipped alongside `.semantic()` as part of KI-018's hybrid-retrieval resolution. `tests/benchmarks/test_hybrid_query.py` exercises `.semantic()` and `.semantic()+.where()` only; neither confidence nor trust filtering is benchmarked, so the regression is invisible to CI.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

Added `StorageBackend.entities_meeting_confidence(namespace, concept, threshold)` / `.entities_meeting_trust(namespace, concept, min_trust)` (both backends) — a single `SELECT DISTINCT a.subject FROM assertion a JOIN entity e ON e.id = a.subject WHERE e.namespace = ? AND e.concept = ? AND ...` per filter (an added `JOIN principal` for trust), returning every qualifying entity id in that `(namespace, concept)` in one query. `QueryBuilder._apply_confidence_trust_filters` intersects this against the already-narrowed candidate list (from `.where()`/`.semantic()`, if chained) — one round trip per active filter, regardless of concept size or candidate count, not one per candidate.

An id-list-bound design (`WHERE id IN (...)`, one placeholder per candidate) was tried first and reverted during review: it hits SQLite's bound-variable limit outright on large concepts (`sqlite3.OperationalError: too many SQL variables` — invisible on this project's dev-machine SQLite build, whose reported limit happens to exceed the 1k-entity benchmark scale, but real on the upstream default of 32766, and much lower on some builds), and costs DuckDB linear per-parameter bind overhead (measured ~83µs/id — a 10k-entity concept scan with both filters chained would cost ~1.7s against the 150ms hybrid-query budget). Scoping by `(namespace, concept)` instead keeps the parameter count constant regardless of data size.

This tradeoff is bidirectional, not a strict improvement: scoping by concept means `.min_confidence()`/`.trust_at_least()` can no longer exploit an already-narrow `.where()`/`.semantic()` candidate set the way the reverted id-list design would have (a `.where()` match narrowed to 1 entity out of 50k measured ~1150x slower under the concept-scan design than the old per-candidate loop would have been for that one candidate). Tracked separately as KI-037 rather than re-introducing the id-list design's own failure mode to chase this back — a hint parameter that a backend may selectively exploit (SQLite can; a naive DuckDB `IN (unnest(?))` measured slower than its own full scan) is the likely fix, and is additive rather than another breaking `StorageBackend` change if landed later.

The two new SQLite methods carry `@_synchronized` like every other public `SQLiteBackend` method (KI-023's invariant) — missing on the first pass, and confirmed via a reproduction that a concurrent call without it returned an uncommitted row from another thread's still-open transaction. `tests/unit/test_sqlite_backend.py::TestConcurrency::test_entities_meeting_confidence_serialized_with_open_transaction` pins this: a reader blocked on an in-flight writer's lock, observing the writer's forced rollback rather than a dirty read.

New benchmarks (`test_bench_min_confidence_full_concept_scan`, `test_bench_trust_at_least_full_concept_scan` in `tests/benchmarks/test_hybrid_query.py`) exercise both filters over a 1k-entity concept scan with no `.where()`/`.semantic()` narrowing — the scenario that made the original N+1 invisible — at ~4-5ms mean, comfortably inside the general symbolic-query budget; informational only, no CI gate, so this alone does not prevent a future reintroduction of the N+1 (a spy-backend unit test asserting call counts would be a stronger regression guard, not added here). New conformance vectors (`conformance/test_confidence_trust_filters.py`) prove both backends implement the push-down identically (the pre-existing unit tests only ever exercised SQLite), including that the two filters compose conjunctively (AND), not disjunctively.

Not addressed (pre-existing, out of scope for this performance fix — tracked as KI-036): `.min_confidence()`/`.trust_at_least()` never threaded `QueryBuilder._as_of_time` through to the confidence/trust check at all, before or after this fix — `kb.as_of(t).query(...).min_confidence(...)` silently evaluates against current-active assertions regardless of `t`. Both builder methods' docstrings now say so explicitly. The new backend methods' signatures deliberately don't reserve an unused `as_of_time` parameter for this — KI-036's fix needs a design decision on what "trust_level as of `t`" even means (principals aren't currently versioned), and a speculative parameter shaped before that decision is made risks being the wrong shape anyway.

---

## KI-029 — MCP `ontolith.schema` and REST `GET /schema` omit `relations` ✓ RESOLVED (M3)

**Severity:** Architecture gap — SPEC-required schema information is unreachable via two of the four primary interfaces
**Milestone target:** M3 — resolved in `feat(interfaces): add relations to MCP/REST schema output (KI-029)`
**SPEC reference:** SPEC §14.4 (`ontolith.schema` MUST "Return concepts/relations/temporality")

### Description

`schema_tool` (`src/ontolith/interfaces/mcp.py:64-103`) iterates `concept_def.properties` only; `ConceptDef.relations` (`src/ontolith/schema/ir.py:58-73`, populated for every schema that declares a `Relation`) is never read. `GET /schema`'s `ConceptOut`/`get_schema` route (`src/ontolith/interfaces/rest.py:98-103`, `:495-...`) has the identical gap — no relations field on the response model at all.

An agent or REST client inspecting the schema this way cannot see that a relation like `Person.employer` exists, or whether it's `time_varying` — exactly the information that predicts whether a subsequent proposal on that predicate will supersede or contradict (SPEC §10.1).

Surfaced during a whole-project milestone audit (2026-07-29); flagged in a prior 2026-07-14 audit and not yet closed.

### Fix

Added a `relations` list (name, target concept, cardinality, required, temporality, inverse) alongside `properties` in both `schema_tool`'s dict output and REST's new `RelationOut` model on `ConceptOut`/`SchemaOut` — mirroring `PropertyOut`'s existing shape. `ConceptOut.relations` is a required field (not defaulted), so any caller still constructing one without it now fails fast at construction rather than silently omitting the field again; the one in-repo call site (`get_schema` route) was updated accordingly. `PropertyOut`/the property dict output also gained `cardinality` in the same pass — an omission of the same class (SPEC §4/ADR-0017: cardinality, not just temporality, predicts contradiction-vs-coexistence for a subsequent proposal), left inconsistent with relations (which already had it) had this not been caught in review.

The CLI has no schema-inspection command at all — genuinely out of scope for this fix (a new CLI command, not an output-shape change), but this text previously claimed it was "tracked separately" under KI-032, which was wrong: KI-032 covers CLI proposal-review commands and never mentions schema. Corrected; the actual gap is now tracked as KI-038 (`ontolith schema show` closed by KI-038; `ontolith schema migrate` remains open, forward-tracked as KI-048).

---

## KI-030 — `QueryBuilder.where()` silently no-ops on relation-traversal filter keys ✓ RESOLVED (M3)

**Severity:** Test gap / DX — a documented example produces an empty result with no error
**Milestone target:** M3 — resolved in `fix(query-store): reject dunder relation-traversal keys, match relation filters on value_ref (KI-030)`
**SPEC reference:** N/A — internal DX/correctness gap, not a SPEC deviation

### Description

`QueryBuilder`'s class docstring (`src/ontolith/query/builder.py:28-31`) advertises `kb.as_of("2025-01-01").query(Person).where(employer__name="Acme Corp")` as a working example; `.where()`'s own docstring (`:63-67`) still says "M2 will add relation traversal." `_qualified_filters()` (`:166-168`) compiles any keyword into the literal predicate string `f"{concept}.{key}"` — `employer__name` becomes the literal predicate `"Person.employer__name"`, which matches no assertion. Even a correctly-named relation-target filter (e.g. `employer="org-id"`) can't match, since `entities_where()`'s SQL (`store/{sqlite,duckdb}/backend.py`) only compares `value_lit`, never `value_ref`. `.where()` accepts any of this silently and returns an empty list — no error, no warning — for a pattern its own docstring calls working, two milestones after the "M2 will add" comment was written.

Surfaced during a whole-project milestone audit (2026-07-29); flagged in a prior 2026-07-14 audit and not yet closed.

### Fix

`.where()` now rejects any dunder-containing key (e.g. `employer__name`) with a `ValidationError` at call time, naming the offending key and explaining that neither multi-hop traversal nor lookup operators are implemented (ADR-0027 records this as a deliberate deferral, not an oversight; see also KI-039) — instead of silently compiling it into `f"{concept}.{key}"`, a predicate string that could never match anything. MCP's `ontolith.query` tool and REST's `POST /query` both already mapped `ValidationError` correctly (400 / structured error) once `.where()` started raising it; only MCP's `query_tool` needed an explicit `try`/`except` added, since it previously called `.where()` outside its existing error-handling block.

Separately, the class docstring's own example used a relation-target filter that — dunder syntax aside — couldn't have worked anyway, since `entities_where()`'s SQL only ever compared `value_lit`. Rather than just deleting the example, `entities_where()` (`store/{sqlite,duckdb}/backend.py`) now matches a filter value against `value_lit` or `value_ref`, so a direct relation-target-id equality filter (`employer="org-123"`) actually returns matches — the class docstring's example was updated to this form, which now genuinely works. `.where()`'s docstring was rewritten to state the real contract: equality on literal properties and on a relation's target id, no traversal.

**Caught in review, fixed before merge:** the first version of the `value_ref` match used a single `value_lit = ? OR value_ref = ?` predicate. Measured against a 100k-assertion SQLite fixture, the `OR` defeated both `idx_assertion_pred_value` and a new `idx_assertion_pred_ref` companion index — the planner fell back to a full table scan on the bitemporal (`as_of`) branch (~9x slower measured; ~600x at 50k rows for a single predicate) — turning every `.where()` call, not just relation filters, into an unindexed scan on the backend the SPEC performance budgets target. Reworked into a `UNION ALL` of two single-column point lookups, one per index, which restored (slightly beat) the pre-fix latency. The SPEC §11.1 example, PRD walkthrough, and a `docs/Ontolith_UseCases_and_Interfaces.md` example (an unrelated but same-shaped `__contains` lookup-operator gap, KI-039) were also brought in line with the actual (equality-only) contract, and ADR-0027 records the traversal/lookup-operator deferral as a decision rather than leaving the SPEC and code in silent disagreement.

---

## KI-031 — Schema `value_type`/`required` are declared but never enforced at write time — PARTIALLY RESOLVED (M3)

**Severity:** Architecture gap — declared schema constraints are silently unenforced
**Milestone target:** M3 — `value_type` resolved; `required` remains open, tracked forward as KI-041/KI-042
**SPEC reference:** SPEC §4 (`value_type` is a core type; `required`/`unique`/`constraints` are validator-backed, §13); Implementation Plan §4.3 ("validate at edges")

### Description

`Ontology._require_known_predicate` (`src/ontolith/ontology.py:397-409`) rejects an unknown predicate at write time but never checks the caller-supplied `value_type` against the schema-declared `PropertyDef.value_type` (`src/ontolith/schema/ir.py:12-27`), and nothing checks `PropertyDef.required`/`RelationDef.required` at all. A predicate declared `value_type: Integer` in schema currently accepts `assert_literal(..., value_type="Text", ...)` without error; a `required: true` property is never checked as present on an entity.

A prior audit (2026-07-14) flagged this as partially open; cardinality is now enforced (`govern/conflict.py`, ADR-0017) and unknown predicates are now rejected, but the `value_type`/`required` remainder was never itself tracked as its own entry.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

**`value_type`: resolved.** `_require_known_predicate` (`src/ontolith/ontology.py`) now takes an optional `value_type` parameter; `assert_literal` and `propose` (the two literal write paths) pass their caller-supplied `value_type` through, and a mismatch against the schema's declared `PropertyDef.value_type` (via new `SchemaIR.value_type_of()`) raises `ValidationError` — same shape and same call sites as the existing unknown-predicate check. `assert_ref`/`propose_ref` are unaffected (relations have no `value_type`). No check fires when the predicate resolves to a relation (`value_type` is a required field on `PropertyDef`, so a property can never itself omit it), or when no schema is registered for the namespace. Enforcement is submission-time only — a proposal is not re-validated against a possibly-changed schema when later accepted/resubmitted, matching the existing, documented precedent that `_require_known_predicate` isn't re-run at replay either.

**`required`: still open, deliberately deferred, not silently dropped.** ADR-0028 records the decision not to add a core-layer required-field presence check — SPEC §4 itself assigns `required` to the validator layer (§13), and structurally a per-write core gate is the wrong shape for it anyway (an entity necessarily fails every required declaration for a moment right after `create_entity`, before its other properties are asserted). This ADR's first draft justified the deferral by pointing at `RequiredFieldsValidator` (KI-010) as an already-working answer; that turned out to be inaccurate on review and was corrected before merge: the plugin's required-predicate set is independently configured, not derived from `PropertyDef.required`/`RelationDef.required` (now tracked as **KI-041**), and more fundamentally, no code path anywhere invokes any registered `Validator.validate()` at all — the protocol exists but nothing calls it (now tracked as **KI-042**). Today `required` is schema metadata, surfaced read-only via `GET /schema`/`ontolith.schema`/codegen, enforced by nobody. See ADR-0028 for the full corrected rationale.

---

## KI-032 — CLI has no `proposal accept`/`reject`/`review` commands ✓ RESOLVED (M3)

**Severity:** Test gap / DX — one of SPEC's four primary interfaces cannot act on its own review queue
**Milestone target:** M3 — resolved in `feat(interfaces): add CLI proposal accept/reject/review/resubmit commands (KI-032)`
**SPEC reference:** SPEC §14.2 (CLI command surface, `proposal {list|review}`)

### Description

`src/ontolith/interfaces/cli.py`'s `proposal_app` has exactly one subcommand, `list` (`:347`). `Ontology.accept_proposal`/`reject_proposal`/`request_changes` all exist, and REST exposes all three (`POST /proposals/{id}/accept|reject|review`), but the CLI — one of SPEC's four primary interfaces alongside SDK/REST/MCP — has no way to act on any of them. An operator using only the CLI can discover what's pending review (`proposal list`) but cannot accept, reject, or request changes on any of it.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

Added `ontolith proposal accept <id> --reviewer`, `proposal reject <id> --reviewer [--reason]`, and `proposal review <id> --reviewer [--reason]` (mapping to `request_changes`; the `review` name is SPEC §14.2's own literal command name, `proposal {list|review}`, not just a REST-route echo), each calling straight through to the corresponding `Ontology` method. `--reviewer` accepts `--author` as an alias — caught in review: `Proposal.author` is a real, distinct field `_require_reviewer` explicitly checks the acting principal is *not* (self-review is rejected), so naming the reviewer option `--author` reads as an assertion guaranteed false on every successful call; `--author` is kept working as an alias rather than a breaking rename. Also added `ontolith proposal resubmit <id> --author` (correctly `--author` here — the resubmitting principal genuinely is the proposal's own author or delegate) — REST/MCP both gained a `resubmit` action alongside accept/reject/review when KI-027 closed the `request_changes` dead end, with CLI parity explicitly deferred to this KI at the time (see `CHANGELOG.md`'s KI-027 entry); closing it here keeps all four proposal-lifecycle actions available from every primary interface, not three of four.

---

## KI-033 — `retract()` lets a party to an open contradiction unilaterally retract the opposing member ✓ RESOLVED (M3)

**Severity:** Architecture gap — same governance outcome as KI-026, reachable through a different method that has no contradiction awareness at all
**Milestone target:** M3 — resolved in `fix(govern): reject retract() of a contradiction member the retracting principal is party to (KI-033)`
**SPEC reference:** SPEC §10.3 (contradiction resolution)

### Description

`Ontology.retract()` routes through `self.policy` like any other governed write; a human principal with `review` (or higher) capability auto-accepts under `ThresholdPolicy`. `retract()` performs no check on whether the target assertion is a `flagged` member of an open contradiction, nor whether the caller is a party to that contradiction (author/delegate of any of its members). A principal who authored one side of a disputed static fact can therefore retract the *opposing* member directly — reproduced: after such a retraction, the contradiction is left `open` with only the retracting party's own value still `flagged`, so a later legitimate resolver effectively has no real choice left to make.

KI-026 closed the front door (`resolve_contradiction` itself); this is a side door reaching a similar outcome through `retract()`, which has no notion of contradictions at all today. Found while fixing KI-026, not introduced by it — pre-existing.

### Fix

New `Ontology._reject_retract_if_party_to_contradiction(assertion_id, parties)`: if the target assertion is currently `flagged` and a member of an open contradiction, raises `CapabilityError` when any of `parties` is the author or delegate of *any* member of that contradiction — mirroring KI-026's "any member, not just one side" reasoning, so retracting your own losing entry is blocked too, not just the opposing one. A missing member (`get_assertion` returns `None`) raises `NotFoundError` rather than silently skipping the check for it, matching `resolve_contradiction`'s own precedent (assertions are append-only and never deleted, so a missing member is corruption, not a benign gap). Runs inside the same transaction that performs the retraction, before any of that transaction's writes land, in both call sites that can execute a `retract`-kind operation — `retract()`'s own auto-accept branch (`parties = {author, acting_as}`), and `_replay_proposal_operations`'s `retract` branch (shared by `accept_proposal`/`resubmit`; `parties = {proposal.author, proposal.acting_as}`, plus the accepting reviewer when called from `accept_proposal` — a reviewer who is themselves a party to the same contradiction can reach the identical one-sided outcome by approving a *neutral* principal's retract proposal, not just by retracting directly, so `parties` must cover whoever's decision actually causes the retraction, not only the original proposer). Matches `resolve_contradiction`'s own reasoning for checking inside the transaction: a contradiction opened or extended concurrently can't slip past a check made only beforehand. A neutral third party (author/delegate of no member) is unaffected by this guard — whether that should itself require `resolve_contradiction`-grade `review`/`admin` capability, rather than plain `write`, is a separate, broader question tracked as KI-043, not expanded into this fix's scope. New conformance vectors in `conformance/test_contradiction_resolution.py::TestRetractContradictionGuard`.

---

## KI-034 — Extending an open contradiction can resurrect an already-`retracted` member back to `flagged` ✓ RESOLVED (M3)

**Severity:** Test gap — a documented lifecycle transition (`retracted` is meant to be terminal) doesn't hold under a specific sequence
**Milestone target:** M3 — resolved in `fix(govern): retracted stays terminal across contradiction extension (KI-034)`
**SPEC reference:** SPEC §5 (assertion lifecycle — `retracted` status)

### Description

Reproduced while investigating KI-033: if an assertion belonging to an open contradiction is retracted (e.g. via the KI-033 gap, or by any other means reaching `retract()`), a subsequent `propose()`/`assert_literal` on the same `(subject, predicate)` that extends the same open contradiction can flip that already-`retracted` assertion's status back to `flagged`. `retracted` is meant to be a terminal status (SPEC §5's append-only lifecycle) — this is the path reachable from the write/proposal pipeline where it wasn't. Review found `flag_contradiction()` has the identical bug via a separate call site (fixed alongside this, below); `resolve_contradiction()` could also resurrect an already-`retracted` member all the way to `active` if picked as the winner — a broader eligibility question, not the same "extend a contradiction" mechanism this fix closes, tracked and since resolved separately as KI-044/ADR-0031 (which also extended this fix's own "retracted is terminal" guard to cover `superseded`, found incomplete during KI-044's own review).

### Fix

The primary bug lives in `Ontology._apply_with_conflict_routing`'s own "extend an already-open contradiction" branch (not `govern/conflict.py`'s pure `route()` — that path is bypassed entirely once a contradiction is already open), which unconditionally re-flagged every id in `open_contradiction.member_ids` alongside the incoming assertion. The `Contradict`-branch flagging loop now looks up each existing member's *current* status and skips the `set_assertion_status(..., "flagged")` write (and its accompanying event) for any member already `retracted` (and, since KI-044/ADR-0031 widened this same branch, `superseded` too) — treating both as final regardless of the contradiction's own open/resolved state. A missing member now raises `NotFoundError`, matching `resolve_contradiction`'s own KI-026 precedent (found in review). `flag_contradiction()` (SPEC §14, MCP `ontolith.flag_contradiction` — reachable at only `propose` capability, including by an AI principal) had the identical resurrection bug via its own, separate flagging loop; it now skips `retracted`/`superseded` members the same way. The member's id is deliberately left in the `Contradiction`'s own `member_ids` — that list isn't audit-only, it's also `resolve_contradiction`'s winner-eligibility set (KI-044/ADR-0031 closed that interaction) and `_reject_retract_if_party_to_contradiction`'s scan set (which — per KI-051, found later — still doesn't recognize a `superseded` member as governed the way it does a `flagged` one); only the re-flagging write is skipped here. `resolve_contradiction` itself also no longer re-emits a duplicate, resolver-misattributed `retracted` event for a loser that's already `retracted` (found in review; later widened to `superseded` too, KI-044). New conformance vectors in `conformance/test_contradiction_resolution.py::TestRetractedIsTerminalAcrossExtension`, including one confirming the primary vector fails without the fix (reverted locally and re-run to verify).

---

## KI-035 — Proposal-transition methods validate state before opening the write transaction (TOCTOU) ✓ RESOLVED (M3)

**Severity:** Architecture gap — data-integrity risk under real concurrent traffic, not yet triggered by any test (single-threaded today)
**Milestone target:** M3 — resolved in `fix(govern): close proposal-transition TOCTOU window (KI-035)`
**SPEC reference:** SPEC §9.1 (proposal state machine)

### Description

`Ontology.accept_proposal`/`reject_proposal`/`request_changes`/`resubmit` (`src/ontolith/ontology.py`) each read the proposal, validate its current state (via `_require_reviewer` for the first three; inline in `resubmit`), and run policy evaluation — all *before* opening `with self.backend.transaction():`. Only the resulting writes are atomic; the read-validate window itself is not. Two concurrent calls that both observe the same pre-transition state (e.g. two `resubmit()` calls both reading `changes_requested`, or an `accept_proposal` racing a `reject_proposal` both reading `require_review`) can both pass validation and both reach the transactional write — under an auto-accepting policy this can replay the same proposal's operations twice.

`resolve_contradiction`'s equivalent gap (found and fixed for KI-026: "a review pass caught that reading `contradiction.member_ids` before the transaction opened a real TOCTOU window") shows the fix pattern: move validation inside the transaction, re-reading the row instead of trusting the pre-transaction snapshot. This is the same class of bug applied to the proposal state machine's four transition methods, none of which received that fix. Found while re-reviewing the KI-027 `resubmit()` fix — pre-existing in `accept_proposal`/`reject_proposal`/`request_changes` before this branch, not introduced by it.

### Fix

`_require_reviewer` split into `_require_reviewer_principal(reviewer)` (auth/capability/AI-kind checks — depend only on the reviewer's own identity, safe to run once before the transaction opens) and `_require_pending_proposal(proposal_id, reviewer)` (re-reads the proposal fresh and checks self-review + state — MUST run inside the transaction, immediately before the write). `accept_proposal`/`reject_proposal`/`request_changes` all now call `_require_pending_proposal` as the first thing inside their `with self.backend.transaction():` block, mirroring `resolve_contradiction`'s own KI-026 fix. `resubmit` keeps its original author/state checks before policy evaluation (needed to construct the object policy evaluates against) as an optimistic fast-fail, but adds a second, authoritative re-read-and-recheck as the first thing inside its own transaction, before `_finalize_non_accepted_decision`/the auto-accept write — so a proposal a concurrent call already moved out of `changes_requested` is detected and rejected rather than double-processed. New conformance vectors in `conformance/test_review_workflow.py::TestProposalTransitionTOCTOU`, one per method: a `_RacingClock` test double (subclasses `FixedClock`) fires a one-shot side effect the first time `.now()` is called — for `accept_proposal`/`reject_proposal`/`request_changes` that's the exact point right before the write transaction opens; `resubmit` calls it slightly earlier (before policy evaluation, itself before the transaction), a marginally wider window but still inside the same gap the fix closes — running a real, complete sibling transition as if it had just won the race, deterministically simulating the TOCTOU window without needing real threads. All four confirmed to fail without the fix (reverted `ontology.py` locally and re-ran). `resubmit`'s in-transaction re-check closes the race on the proposal's *state* only, not on the `kb_view` its policy decision was evaluated against outside the transaction — a pre-existing, deliberate tradeoff (per this KI's own Fix text) shared by `propose`/`propose_ref`/`retract`, not something newly closed here.

Review found the identical TOCTOU shape in `flag_contradiction()` (reads two assertions and any existing open contradiction before its transaction, filed separately as KI-045) and that `DuckDBBackend` has no equivalent of `SQLiteBackend`'s KI-023 lock, so this fix's serialization guarantee is airtight only for SQLite today (filed as KI-046) — neither expanded into this fix's scope.

---

## KI-036 — `.min_confidence()`/`.trust_at_least()` ignore `.as_of()` ✓ RESOLVED (M3)

**Severity:** Architecture gap — bitemporal query results are inconsistent within a single query
**Milestone target:** M3 — resolved in `fix(query): thread as_of_time through confidence/trust filters (KI-036)`
**SPEC reference:** SPEC §12 (bitemporal query semantics — `as_of` reconstruction)

### Description

`QueryBuilder._apply_confidence_trust_filters`/`_passes_confidence_trust` (`src/ontolith/query/builder.py`) never reads `self._as_of_time`. `kb.as_of(t).query("Person").where(...)` correctly threads `t` through `_base_candidates()` (both `entities()` and `entities_where()` accept `as_of_time`), but `.min_confidence()`/`.trust_at_least()` chained onto the same query always check current-active assertions/principals regardless of `t` — an entity can pass the `.where()` half of the query as it existed at `t`, then get filtered by confidence/trust values that only became true after `t` (or that existed at `t` but were later superseded/retracted). The result is not a coherent point-in-time view.

Found while fixing KI-028 (the N+1 performance issue for the same two filters); pre-existing before that fix and unchanged by it — the new `entities_meeting_confidence`/`entities_meeting_trust` backend methods intentionally preserve the old (non-bitemporal) behavior rather than silently changing query semantics inside a performance-only fix.

### Fix

`StorageBackend.entities_meeting_confidence`/`entities_meeting_trust` (port + both backends) gained an `as_of_time: datetime | None = None` parameter, and `QueryBuilder._apply_confidence_trust_filters` now passes `self._as_of_time` through to both. When set, each method switches from `status = 'active'` to the same bitemporal window `entities_where()` already uses (`asserted_at <= as_of_time`, `valid_from`/`valid_to` bracketing it) — so the qualifying assertion is whichever one was actually active at `t`, not whichever is active now. One caveat, since `status` itself is not bitemporally versioned: a `status = 'flagged'` assertion (an open static contradiction) is excluded regardless of `t`, even at a `t` before it was flagged — matching `entities_where()`'s own default (`include_flagged=False`), the only mode `QueryBuilder` ever reaches.

The design question this KI flagged — what "trust_level as of `t`" means, since principals aren't versioned — resolved to: `trust_level` is always the principal's *current* value, never a historical one. This isn't an approximation: no code path anywhere updates a principal's `trust_level` after creation (confirmed by grep — the only `UPDATE principal*` statements touch `principal_credential`, never `principal` itself), so "trust_level as of any t at or after the principal's creation" and "trust_level now" are provably the same number. Only which *assertion* counts as qualifying is bitemporally scoped; the trust threshold it's compared against is not, because there is nothing to reconstruct. A new regression guard, `tests/unit/test_principal_trust_immutability_invariant.py`, greps both backends for any `UPDATE`/`DELETE FROM` targeting the `principal` table and fails if one is ever added — the prompt that this shortcut needs replacing with real principal versioning before `.trust_at_least()` + `.as_of()` can keep relying on it.

New conformance vectors in `conformance/test_confidence_trust_filters.py::TestAsOfConfidenceTrust` (22 cases across both backends): a retraction-boundary pair and a schema-declared `Person.employer` time_varying supersession-boundary pair per filter (as originally landed); plus, per review, boundary vectors isolating each of the four temporal clauses individually (backdated-but-not-yet-known, future `valid_from`, the `valid_to` half-open boundary, and flagged-exclusion) per filter, and vectors combining `.as_of()` with `.where()` and with both filters chained together. Mutation-testing each clause in the SQLite backend (blanking `status != 'flagged'`, `valid_from`, `valid_to`, and `asserted_at` one at a time and rerunning the suite) confirms every clause is now individually pinned by a failing test, not just exercised incidentally.

---

## KI-037 — `.min_confidence()`/`.trust_at_least()` can't exploit an already-narrowed `.where()`/`.semantic()` candidate set ✓ RESOLVED (M3)

**Severity:** Performance — no budget violated today, but a real, measured slowdown relative to the design it replaced for the selective-query case
**Milestone target:** M3 — resolved in `perf(query): let entities_meeting_confidence/trust exploit a narrowed candidate set (KI-037)`
**SPEC reference:** Implementation Plan §9 (performance budgets)

### Description

KI-028's fix scoped `entities_meeting_confidence`/`entities_meeting_trust` by `(namespace, concept)` rather than an explicit candidate id list, specifically to keep the SQL parameter count constant regardless of data size (an id-list-bound design was tried first and reverted — see KI-028's own Fix text). The tradeoff: when `.where()` or `.semantic()` has already narrowed the candidate set to a small fraction of the concept (the common case for `.semantic()`, which caps overfetch at `_MAX_OVERFETCH = 1000` regardless of concept size), `.min_confidence()`/`.trust_at_least()` still scan the *entire* `(namespace, concept)` rather than just the narrowed candidates — measured at ~1150x slower than the old per-candidate N+1 loop for a single-candidate `.where()` match against a 50k-entity concept (46ms vs ~0.04ms), and estimated to consume a meaningful fraction of the 150ms hybrid-query budget at 100k+ entities when chained after `.semantic()`.

This is not a regression relative to *shipped* behavior (the id-list design that would have preserved this property was never released — it was caught and reworked during KI-028's own review, before merge), but it is a real, measured cost of the design actually shipped, worth its own tracked follow-up rather than silently living only in KI-028's fix-note prose. `tests/benchmarks/test_hybrid_query.py::test_bench_min_confidence_narrowed_by_where` benchmarks this scenario (informational only, no CI gate) — at the 1k-entity scale used there the cost is small (SQLite's query planner still picks an index-backed scan), so the effect is real but not yet visible at benchmark scale; the 46ms figure above was measured separately at 50k entities.

### Fix

`StorageBackend.entities_meeting_confidence`/`entities_meeting_trust` (port + both backends) gained a **breaking** `candidate_ids: frozenset[str] | None = None` parameter — `QueryBuilder` now passes it as an explicit keyword on every call (including `None`), so any third-party `StorageBackend` must add it (an earlier draft of this fix called it "additive, not breaking"; that was wrong — see CHANGELOG). `QueryBuilder._apply_confidence_trust_filters` passes a non-`None` value only when `.where()`/`.semantic()` narrowed the base candidate set *and* that set is no larger than a new `_CANDIDATE_HINT_MAX = 1000` constant — never on a bare full-concept query (no benefit), and never on a large narrowed set (see below for why that matters).

SQLite binds the id set as a single JSON-encoded parameter (`AND e.id IN (SELECT value FROM json_each(?))`) rather than one placeholder per id; `DuckDBBackend` uses the equivalent `AND e.id IN (SELECT unnest(?))`. Both backends now genuinely narrow — an earlier draft of this fix left `DuckDBBackend` ignoring the hint entirely, reasoning that `unnest()`-based narrowing "measured slower than DuckDB's existing unscoped scan"; that reasoning didn't hold up under review at a wider range of scales: `unnest()` is 2-3x *faster* than the unscoped scan at 10k-50k entities for a small candidate set, and only slower at very small (1k) scale where everything is already fast in absolute terms — but it is a measured **>100x regression** for a candidate set of a few thousand against a 10k-entity concept, which is why `_CANDIDATE_HINT_MAX` exists: bounding the hint's size, not avoiding `unnest()`/`json_each` altogether, is what makes narrowing a reliable win on both backends. SQLite has an analogous (if less severe) crossover: narrowing is a clear win through several thousand candidates against a 50k-entity concept, and a measured regression past ~15-25k.

Measured like-for-like — same query, same fixture, same machine, code-only diff, 50k entities, single-candidate `.where()` match — this fix consistently measures several times faster (absolute latency varies by run/hardware, so no specific ratio is recorded here; see `tests/benchmarks/test_hybrid_query.py`'s `test_bench_min_confidence_narrowed_by_where_50k` vs `..._without_candidate_ids` for a reproducible comparison). An earlier draft of this benchmark claimed a much larger, ~17x win by comparing against a full-concept-scan benchmark returning 25,000 results instead of the same 1-result query with narrowing suppressed — an apples-to-oranges comparison dominated by result-set materialization cost, not by `candidate_ids`; the two benchmarks now exist side by side specifically so that mistake isn't repeated. Not O(1) on SQLite: `EXPLAIN QUERY PLAN` shows it still `SEARCH`es the full `(namespace, concept)` range of the `entity` index before bloom-filtering against `candidate_ids` — only the assertion-side join is pruned.

New conformance vectors (`TestCandidateIdsNarrowing` in `conformance/test_confidence_trust_filters.py`, cross-backend) pin the backend-agnostic contract that holds regardless of whether/how a given backend narrows: `(full_qualifying_set & candidate_ids) <= narrowed <= full_qualifying_set` — a narrowed call never returns a non-qualifying entity, and never drops a candidate that genuinely qualifies. Backend-specific "does narrowing actually narrow" vectors (`TestCandidateIdsNarrowing` in both `tests/unit/test_sqlite_backend.py` and `tests/unit/test_duckdb_backend.py`) pin each backend's own implementation with exact-equality assertions, including an empty-`candidate_ids` short-circuit — these, plus the conformance vectors above, are what actually regression-pin the fix (confirmed to fail reverted to pre-KI-037 `main`). A `.semantic()`-narrowed (not just `.where()`-narrowed) end-to-end vector was also added to `tests/unit/test_query.py`'s `TestSemanticSearch` class for branch coverage of the gating condition's `self._semantic_text is not None` disjunct — this one is *not* a regression pin (like its pre-existing `.where()`-based sibling, `_apply_confidence_trust_filters`'s local re-intersection makes the end-to-end outcome invariant to whether `candidate_ids` is threaded at all; the test still passes reverted to `main`), and its docstring says so.

Docstrings on both port methods were rewritten: `(namespace, concept)` is still always the mandatory scope (this is what bounds the parameter count — see KI-028's own reverted id-list design), and `candidate_ids` is an optional narrowing hint layered on top, not a replacement for it.

---

## KI-038 — CLI has no `schema` command — PARTIALLY RESOLVED (M3)

**Severity:** Architecture gap — SPEC-normative CLI surface is entirely unimplemented
**Milestone target:** M3 for `show` (resolved in `feat(cli): add schema show command (KI-038)`); `migrate` forward-tracked as KI-048
**SPEC reference:** SPEC §14.2 (`ontolith schema {show|migrate}`)

### Description

`src/ontolith/interfaces/cli.py` registers `principal`/`entity`/`proposal`/`contradiction`/`namespace` sub-apps plus top-level `assert`/`assertions`/`query`/`reindex` commands — no `schema` command at all, despite SPEC §14.2 normatively listing `ontolith schema {show|migrate}` as part of the CLI surface. Found while fixing KI-029 (MCP/REST schema output), which had initially described this gap as "tracked separately... KI-032 covers CLI proposal-review commands specifically" — that framing was wrong: KI-032 doesn't mention schema at all, so the gap had no actual tracked issue until now.

### Fix

Added `ontolith schema show [--namespace]` (new `schema_app` sub-app, matching every other CLI sub-command's `_kb()`/try-except-finally shape), printing `namespace=... version=...` followed by each concept's properties (`name: value_type  cardinality=...  temporality=...  required=...`) and relations (`name -> target_concept  cardinality=...  temporality=...  required=...  inverse=...`) — the same field set as MCP's `ontolith.schema`/REST's `GET /schema` output (both already fixed by KI-029 to include relations), though not their exact attribute order: REST's own `PropertyOut`/`RelationOut` don't even agree with each other on relation field order, so the CLI normalizes to one consistent order instead of copying either verbatim. A namespace with no registered schema prints a plain message rather than an empty/error output, matching other read-path commands' "nothing found" convention elsewhere in the CLI.

`ontolith schema migrate` remains unresolved — it's a larger, separate piece of work (schema versioning/migration isn't implemented anywhere yet, only monotonic version numbering via `apply_schema`) and was out of scope for this pass, as this KI's own original Fix text anticipated ("split into its own issue if `show` lands first"). Forward-tracked as **KI-048**, matching the pattern KI-031 set for its own leftover half, rather than leaving it implicit in this entry's own partially-resolved status.

---

## KI-039 — `QueryBuilder.where()` has no lookup-operator syntax (e.g. `__contains`), despite a documented example using one ✓ RESOLVED (M3)

**Severity:** Documentation/DX gap — a documented use-case example used a filter shape the code never implemented
**Milestone target:** M3 — resolved in `feat(query): add __contains/__gt/__lt/__gte/__lte lookup operators to .where() (KI-039)`
**SPEC reference:** N/A — not covered by SPEC §11.1's normative shape; only appeared in a worked example

### Description

`docs/Ontolith_UseCases_and_Interfaces.md` §4.3 used `kb.query(Claim).where(text__contains="Compound X")` to illustrate a substring/lookup-style filter. `.where()` has never supported any lookup-operator syntax — only bare equality — so this example was exactly as broken as KI-030's `employer__name` traversal example: a double-underscore key that (pre-KI-030) silently compiled into an unreachable predicate and matched nothing, and (post-KI-030) now raises `ValidationError` instead, same as any other dunder key.

Surfaced while fixing KI-030 and drafting ADR-0027, which defers both traversal and lookup operators as a single scope decision; the use-case doc was corrected to an equivalent equality-based example in the same pass, but implementing `__contains` (or any other operator) itself was out of scope for that fix.

### Fix

`.where()` now recognizes a closed set of five lookup-operator suffixes: `__contains` (substring match) and `__gt`/`__lt`/`__gte`/`__lte` (numeric range). Multi-hop relation traversal (a hypothetical `employer__name=`, ADR-0027) and any dunder suffix outside this set are still rejected with `ValidationError`, unchanged from KI-030.

Design decisions this needed, per the Fix text's own open questions:

- **How operators compose with relations**: they don't. `__contains`/range operators only ever check `value_lit`, never `value_ref` — plain equality keeps its existing dual-branch (`value_lit` OR `value_ref`) behavior, matching KI-030. This falls out for free for range operators: `SchemaIR.value_type_of()` returns `None` for a `RelationDef` (relations have no `value_type`), and range operators require a schema-declared `Integer`/`Float` value_type (see next point), so a relation predicate is rejected the same way a `Text`-typed property is — no special-case code needed.
- **Numeric correctness**: `value_lit` is always stored as `TEXT` (SPEC §12.2), so `"9" > "10"` lexicographically but not numerically. Rather than silently produce wrong comparisons, `__gt`/`__lt`/`__gte`/`__lte` are restricted to predicates the active schema declares `Integer` or `Float` — `QueryBuilder` validates this eagerly (at `.where()` call time, via `SchemaIR.value_type_of()`) and raises `ValidationError` otherwise (no schema registered, predicate undeclared, non-numeric value_type, or a value that doesn't parse as a number). The backend then safely does `CAST(value_lit AS REAL)` (SQLite) / `CAST(value_lit AS DOUBLE)` (DuckDB — this module's own header comment already notes DuckDB's `REAL` is 4-byte single precision, unlike SQLite's always-8-byte `REAL`, which would silently round values).
- **Multiple operators on the same predicate** (e.g. an age range needs both `age__gte` and `age__lt`): `StorageBackend.entities_where`'s `predicate_filters` parameter changed from `dict[str, str]` (predicate → value) to `list[tuple[str, str, Any]]` (`(predicate, operator, value)` triples) — a dict keyed by predicate alone couldn't represent two different operators targeting the same predicate. **Breaking** change to the `StorageBackend` Protocol; any third-party implementation must update. `QueryBuilder._filters` is similarly now keyed by `(property, operator)` rather than bare property, so `.where(age__gte=18).where(age__lt=65)` composes as AND while `.where(name="Ada").where(name="Grace")` still overwrites (last value wins), matching equality's pre-existing behavior.

Both backends' `entities_where()` were refactored from duplicated as_of/non-as_of branches into a single loop building each filter's SQL fragment by operator — a side effect that also removed pre-existing code duplication, not just added new operator branches. `__contains` uses a parameterized `LIKE ... ESCAPE '\'` with `%`/`_`/`\` escaped in the search term, so a literal `%` or `_` in a search value can't act as an unintended wildcard.

New conformance vectors (`conformance/test_where_lookup_operators.py`, cross-backend) prove both backends implement every operator identically, including that numeric comparison is genuinely numeric (`"9" > "10"` lexicographically, `9 < 10` correctly under `__gt`) and that LIKE wildcards are escaped. Unit vectors (`tests/unit/test_query.py`) cover operator composition, the schema-validation error paths, and that the still-rejected cases (relation traversal, unrecognized suffixes) keep failing loudly. All new/updated vectors confirmed to fail without the fix (reverted the four touched `src/` files to pre-KI-039 `main` and reran).

`docs/Ontolith_UseCases_and_Interfaces.md` §4.3's example was restored to `where(text__contains="Compound X")`, since the caveat it carried since KI-030/ADR-0027 no longer applies.

**Review found and this fix now closes**: `__contains` was genuinely not identical across backends as first shipped — SQLite's `LIKE` is case-insensitive by default, DuckDB's is not, so the same `.where(name__contains="ada")` call matched different result sets per backend. `SQLiteBackend` now sets `PRAGMA case_sensitive_like = ON` at connection time to match DuckDB's default rather than the reverse. `.where(x__contains=<non-str>)` and `.where(x__gt=True)` (bool is a subclass of `int`, so `float(True) == 1.0` would otherwise silently accept it) now raise `ValidationError` eagerly instead of a bare `AttributeError` leaking from inside the storage adapter for the former. A leading-dunder key with an empty property name (e.g. `.where(__contains="x")`) is now rejected instead of silently compiling to an unmatchable `"Concept."` predicate — the exact KI-030 failure shape. `_coerce_range_value` now resolves the schema via `get_schema_at()` when `.as_of()` is pinned, not always today's schema (SPEC §11.4) — a predicate retyped since `t` is judged by what it was declared at `t`. ADR-0027 was amended (not superseded — multi-hop traversal remains deferred and its rationale still holds) to record that the lookup-operators half of its deferral is resolved; `docs/Ontolith_SPEC.md` §11.1 and the REST/MCP/CLI filter docs (which had drifted to claim equality-only, since all three splat caller filters straight into `.where()`) were corrected to match.

**Review found and filed separately, not fixed here**: nothing validates that a literal's stored *content* actually parses as its predicate's declared `value_type` (KI-031 only checks the type *token* matches) — `CAST(value_lit AS REAL)` on a non-numeric stored value therefore returns `0.0` on SQLite (wrong, silent) rather than excluding the row; DuckDB uses `TRY_CAST` instead of `CAST` specifically to avoid the alternative failure mode (a raw `duckdb.ConversionException` escaping through the port, violating SPEC §16), but that only prevents the crash, not the underlying cross-backend divergence in which rows match. Filed as **KI-049**.

---

## KI-040 — Nothing validates that a predicate's declared kind (property vs. relation) matches how it's written or filtered ✓ RESOLVED (M3)

**Severity:** Architecture gap — a narrow, currently-theoretical write-time/read-time consistency gap
**Milestone target:** M3 — resolved in `fix(ontology): reject predicate-kind mismatches at write time (KI-040)`
**SPEC reference:** SPEC §4 (Schema — properties and relations are distinct declaration kinds)

### Description

`SchemaIR.has_predicate`/`_resolve_field` (`src/ontolith/schema/ir.py`) resolve a predicate name against *either* `ConceptDef.properties` or `ConceptDef.relations` with no kind check propagated back to the caller. `Ontology._require_known_predicate` (used by both `assert_literal` and `assert_ref`) only checks that the predicate is declared *somewhere* in the schema — nothing stops `assert_literal(subj, "Person.employer", "Acme Corp", "Text", ...)` from writing a literal value under a predicate the schema declares as a relation, or the reverse (`assert_ref` writing a `value_ref` under a property-declared predicate).

This was surfaced during KI-030's review: `entities_where()`'s `value_lit`/`value_ref` union match can't distinguish a predicate's intended kind either — it matches both columns unconditionally for every filter, so if a predicate ever did carry mixed-kind assertions (via the gap above), `.where()` would match across both without the caller being able to tell which kind actually matched. Today this is narrow and mostly theoretical: nothing in the codebase currently writes mixed-kind assertions under one predicate, so the union match is safe in practice, not just in theory-free-today.

### Fix

New `SchemaIR.kind_of(predicate) -> Literal["property", "relation"] | None`, mirroring `value_type_of`'s existing shape (returns `None` for an unresolvable predicate — schema-less namespace or undeclared field). `Ontology._require_known_predicate` gained an `expected_kind` keyword-only parameter; when given, it rejects a kind mismatch with a clear `ValidationError` naming the declared kind and the kind the write actually is. `assert_literal`/`propose` pass `expected_kind="property"`; `assert_ref`/`propose_ref` pass `expected_kind="relation"`. `expected_kind` is passed explicitly by each call site rather than inferred from whether `value_type` is set (which would have worked today, since every literal caller happens to pass both together) — keeping the two checks independently reasoned about rather than coupling them through an incidental correlation. No-op for a schema-less namespace, matching `_require_known_predicate`'s existing precedent for the unknown-predicate and value_type checks. `_replay_proposal_operations` (the shared path behind `accept_proposal`/`resubmit`) deliberately does not re-run `_require_known_predicate` at all — pre-existing precedent this fix inherits unchanged, documented at `resubmit`'s own docstring.

A pre-existing conformance test (`TestValueTypeMismatchRejected::test_relation_declared_predicate_has_no_value_type_to_mismatch`) had itself pinned the gap as "deliberately out-of-scope (KI-040)" and asserted the old, now-incorrect behavior (a literal write under a relation-declared predicate silently succeeding) — updated to assert the new `ValidationError` instead, plus a new `TestPredicateKindMismatch` class (10 cases across both backends) covering all four write paths, the matching-kind regression guard, and the no-schema-registered permissive case. Fixing this also surfaced a **real, pre-existing bug** in two unrelated tests (`TestValidityWindowWriteAPI::test_assert_ref_explicit_window_persisted`/`test_propose_ref_reviewed_explicit_window_survives_replay`): both called `assert_ref`/`propose_ref` against `Person.employer`, a property-declared (not relation-declared) predicate in `test_conflict.py`'s shared `_kb()` schema — silently permitted before this fix, now correctly rejected. `_kb()` gained a genuine `Person.manager -> Person` relation for these tests to target instead. New unit vectors (`tests/unit/test_schema_ir.py::TestKindOf`) cover `kind_of()` directly. All new/changed vectors confirmed to fail without the fix.

`entities_where()`'s optional follow-up — selecting `value_lit` vs. `value_ref` per-filter based on the predicate's declared kind, rather than always matching both via `UNION ALL` — was evaluated and deliberately not done, but not because the union is now redundant: the write-time guarantee this fix adds is per-schema-version and forward-only, so it does NOT make the union provably safe to remove. Three cases keep it load-bearing: (1) schema-less namespaces, where kind can't be known and both writes and reads are explicitly permitted regardless of kind (see `test_permitted_without_a_registered_schema`); (2) schema drift — `apply_schema` enforces only monotonic versioning, not backward compatibility, so a predicate declared a property in v1 and a relation in v2 can genuinely hold both `value_lit` and `value_ref` rows, both real and both queryable via `.include_history()`/`.as_of()`; (3) data written before this fix shipped, which is grandfathered. Tightening the union to a kind-based branch would silently break historical relation/property filters in all three cases — left alone deliberately, not because there's nothing left to protect against.

---

## KI-041 — `RequiredFieldsValidator` doesn't read the schema's `PropertyDef.required`/`RelationDef.required` ✓ RESOLVED (M3)

**Severity:** Architecture gap — a schema author's `required=True` declaration has no observable effect anywhere, including in the one plugin whose job description matches it
**Milestone target:** M3 — resolved via ADR-0029, alongside KI-042
**SPEC reference:** SPEC §4 (`required` — validator-backed, §13), §13.2 (Validator protocol)

### Description

`RequiredFieldsValidator` (`src/ontolith/plugins/reference/required_fields_validator.py`) checks whether an entity has all of a *separately, independently configured* set of required predicates (`required_predicates`, a constructor argument defaulting to `{"Person": ("name",)}`) — it never reads `SchemaIR`/`PropertyDef.required`/`RelationDef.required` at all. A schema author who declares `PropertyDef(name="ssn", value_type="Text", required=True)` gets no enforcement from this plugin unless a deployment separately, manually, redundantly re-declares `"ssn"` in the plugin's own `required_predicates` mapping — the two `required` declarations (schema's and the plugin's) are unrelated in code, easy to let drift out of sync, and nothing points out that they should agree.

Surfaced during KI-031's review (2026-08-04): ADR-0028's first draft justified deferring core-layer `required` enforcement by claiming this plugin "already does this correctly" — it does a presence/absence check, but not *the schema's* presence/absence check.

### Fix

`RequiredFieldsValidator.from_schema(schema: SchemaIR)` — a new classmethod that scans every concept's `properties`/`relations` for `required=True` and collects their bare field names into a `required_predicates` mapping, so a schema's own declaration drives the check instead of a hand-maintained, independently drifting one. `RequiredFieldsValidator(...)`'s existing constructor and its small worked-example default (`{"Person": ("name",)}`) are unchanged — `from_schema()` is an alternative constructor, not a replacement.

This has an observable effect end to end only combined with KI-042's resolution (ADR-0029): a deployment wires `Ontology(completeness_validators=[RequiredFieldsValidator.from_schema(schema)])`, and `accept_proposal` runs it. New tests: `tests/unit/test_reference_required_fields_validator.py::TestFromSchema` covers the derivation itself (properties, relations, `required=False` fields excluded, concepts with no required fields absent from the resulting mapping); `tests/unit/test_ontology_validators.py` covers the end-to-end path through `accept_proposal`.

---

## KI-042 — No code path invokes registered `Validator` plugins; `Validator.validate()` is unreachable ✓ RESOLVED (M3)

**Severity:** Architecture gap — an entire plugin protocol category (`Validator`) is wired to nothing
**Milestone target:** M3 — resolved via ADR-0029
**SPEC reference:** SPEC §13.2 (Validator protocol)

### Description

`PluginRegistry` (`src/ontolith/plugins/registry.py`) registers plugins, resolves their capability-scoped views, and enforces the manifest/capability model (ADR-0015) — but nothing in `src/ontolith/` ever calls `.validate()` on a registered `Validator` plugin. `RequiredFieldsValidator` (KI-010) implements the `Validator` protocol correctly and is registrable, but is never actually run: it isn't consulted during `assert_literal`/`assert_ref`/`propose`/`propose_ref`, `accept_proposal`, or anywhere else. Grepping `src/` for `.validate(` (the protocol's one method) turns up zero call sites outside the protocol/plugin definitions themselves.

Surfaced during KI-031's review (2026-08-04) — ADR-0028's first draft described this plugin as "already wired into the `Validator` protocol," which conflated *implementing* the protocol with being *invoked* by anything.

### Fix

ADR-0029 records the decision and the structural conflict that shaped it: a single "run validators synchronously at write time" mechanism cannot serve both single-assertion validators and `RequiredFieldsValidator`'s whole-entity-completeness shape (see KI-041's own description — an entity built up one assertion at a time is incomplete by construction until its last write). Resolved with two separate, independently configured `Ontology` constructor parameters:

- **`validators: Sequence[Validator]`** — per-assertion, synchronous, blocking, run immediately before an assertion commits, at *every* point one actually does: `assert_literal`, `assert_ref`, `propose`/`propose_ref`'s auto-accept branch, and `_replay_proposal_operations` (shared by `accept_proposal` and `resubmit`'s auto-accept branch) — so a proposal that went through review isn't silently exempt just because it skipped the auto-accept branch. A failing validator raises `ValidationError`, aborting the write.
- **`completeness_validators: Sequence[Validator]`** — whole-entity, run once per distinct subject touched by an accepted proposal's operations, only from `accept_proposal`, after all of that proposal's writes have landed in the same transaction. This is where `RequiredFieldsValidator`/`RequiredFieldsValidator.from_schema()` (KI-041) is meant to be wired. Not run on direct writes or any auto-accept path (`propose`/`propose_ref`/`resubmit`) — those bypass human review, an explicit, accepted scope limitation (see ADR-0029's Consequences), not an oversight. A proposal's `retract` operations count as "touching" their target's subject too (review found the initial version silently excluded them, which would have let a retraction make an entity incomplete again with `accept_proposal` never noticing) — only that subject's `.subject` is contractually meaningful in the completeness path, documented directly on the `Validator` protocol.

`Validator.validate()`'s `kb` parameter type widened from the concrete `ReadOnlyView` to a new minimal structural `ValidatorKbView` Protocol (`plugins/ports.py`) — needed because `Ontology` passes itself (not a `ReadOnlyView`) as `kb` for validators registered this way (trusted the same way `PolicyStrategy` already is, ADR-0018 — not sandboxed, since these are deployment-configured, not `PluginRegistry`-loaded third-party plugins), and typing `Ontology`'s own constructor parameters against the concrete `Validator` protocol without a runtime circular import needed the same structural-Protocol technique `govern/policy.py`'s `KbView` already uses (KI-017, ADR-0025) — `ontology.py` imports `Validator`'s type under `TYPE_CHECKING` only. `PluginRegistry`-loaded validators are untouched by this fix and still have no automatic invocation point of their own (recorded as an explicit follow-up in ADR-0029, not a new KI). SPEC §13.2's literal `kb: ReadOnlyView` signature is deliberately left unmatched, mirroring ADR-0025's identical, already-accepted precedent for `PolicyStrategy`.

New tests: `tests/unit/test_ontology_validators.py` covers both lists across all five commit points (direct writes, auto-accept, reviewed accept_proposal, resubmit's auto-accept correctly running `validators` but never `completeness_validators` — the initial version of this test used a vacuously-passing double and wouldn't have caught a regression, fixed after review to use an always-objecting one), a validator rejecting a write correctly rolling back the transaction (including the proposal state reverting on `resubmit`'s auto-accept), multiple validators/multiple error messages aggregating into one `ValidationError`, a `retract` op correctly re-triggering a completeness failure, a multi-subject proposal (hand-built, since `propose`/`propose_ref` only ever create single-operation proposals) checking each subject independently, and the `assert_ref` branch of proposal replay exercised with a validator configured.

---

## KI-043 — `retract()` lets any `write`-capability principal unilaterally shrink an open contradiction, unlike `resolve_contradiction()` ✓ RESOLVED (M3)

**Severity:** Architecture gap — a review-grade governance action (disturbing a disputed static fact) is reachable at a lower capability floor through a different method
**Milestone target:** M3 — resolved via ADR-0030
**SPEC reference:** SPEC §10.3 (contradiction resolution — routed to review, not auto-resolved)

### Description

`resolve_contradiction()` requires `review`/`admin` capability and blocks AI-kind resolvers (SPEC §10.3: disputed static facts are adjudicated by review, not auto-resolved). `retract()` has no such floor — any human/service principal with plain `write` capability auto-accepts under `ThresholdPolicy` for *any* retraction, including one targeting a `flagged` member of an open contradiction. KI-033 closed the *self-dealing* half of this (a party to the contradiction can no longer retract a member they're a party to — see KI-033, and `_reject_retract_if_party_to_contradiction`), but a **neutral** `write`-capability principal — party to neither disputed value — can still retract one member outright, leaving the contradiction `open` with a `retracted` member and no path back to a normal resolution outcome (KI-034 separately ensures that retracted member stays terminal rather than being resurrected if the contradiction is later extended, but doesn't address the capability floor itself). `conformance/test_contradiction_resolution.py::TestRetractContradictionGuard::test_neutral_third_party_can_still_retract_flagged_member` pins this as current, intentional-for-now behavior.

Found during KI-033's review (2026-08-05): the party-to-contradiction check that review closed the loop on is a narrower guard than the capability floor `resolve_contradiction()` itself enforces, and nothing currently states that gap is deliberate.

### Fix

ADR-0030: retracting a `flagged` member of an open contradiction now requires the same `review`/`admin` + non-AI floor `resolve_contradiction()` enforces — but a principal below that floor is **routed to review, not rejected outright**. `retract()`/`resubmit()` substitute a `RequireReview` decision for whatever `self.policy` would otherwise decide, before ever opening a write transaction, whenever the target is a flagged contradiction member the principal can't clear the floor for. A `review`-capable, non-AI principal can then accept the resulting proposal via `accept_proposal()` — the same review queue every other under-capability proposal already uses.

This two-step design (route-to-review, not raise) replaced an initial version of the fix that raised `CapabilityError` unconditionally — review found that left a `write`-capability principal strictly *worse off* than a merely `propose`-capability one for the identical action: `ThresholdPolicy` auto-accepts `write` unconditionally, so the `write`-capability principal's retraction was flatly rejected with no way to get a proposal into the review queue at all, while a lower-capability principal's retraction sailed through the ordinary review queue untouched. New `Ontology._retract_op_review_override`/`_meets_retract_contradiction_floor`/`_open_contradiction_if_flagged_member` implement the routing; `_require_capability_to_retract_flagged_member` (the original raising check) still exists but only as an authoritative, in-transaction backstop for the narrow race where a contradiction opens concurrently between the optimistic routing check and the write transaction.

The party-to-contradiction guard (KI-033) must run *before* the capability-floor routing, and only within the branch where policy would otherwise auto-accept — getting this ordering right (found via two rounds of test failures during review) preserves KI-033's own established behavior: a party is blocked unconditionally regardless of capability (routing them to review instead would just defer an already-certain rejection), while a low-capability, non-party author's proposal that's already headed to review for unrelated reasons skips both checks at submission time, deferring to `accept_proposal()` — exactly as it always has.

Called from `retract()`'s own would-auto-accept branch and `resubmit()`'s own would-auto-accept branch; `_require_capability_to_retract_flagged_member`'s race-only backstop is threaded a `retracting_principal` pair from `resubmit()`'s already-resolved principal/delegate rather than re-deriving them (an earlier version's re-derivation failed *open* on a missing `acting_as`, found in review).

`conformance/test_contradiction_resolution.py::TestRetractContradictionGuard::test_neutral_third_party_can_still_retract_flagged_member` renamed to `test_neutral_write_capability_principal_is_routed_to_review` and rewritten to assert the `RequireReview` routing followed by a successful `accept_proposal()`; new sibling tests cover a neutral `review`-capability principal still auto-accepting directly, an AI-kind principal being routed to review even with `review` capability (via a test-double `PolicyStrategy`, since the default `ThresholdPolicy` already routes every AI-authored proposal to review before this check would ever run), and delegation attenuation. Five other pre-existing tests in this file that used a `write`-only neutral principal purely as retract() setup (not testing the capability floor itself) were updated to a `review`-capability principal so they keep exercising what they were actually about. `tests/unit/test_retract_contradiction_capability_floor.py` covers the `resubmit`-path routing and directly unit-tests the race-only backstop method (both raise branches, and its no-op for ordinary retraction), since the actual race isn't practical to construct end-to-end.

**Breaking:** a deployment automating retraction of flagged contradiction members with only `write`-capability principals will now get back a `require_review` proposal instead of an immediately-retracted assertion — the retraction doesn't take effect until a `review`-capable, non-AI principal accepts it. Ordinary (non-contradiction) retraction is unaffected. Flagged in CHANGELOG per ADR-0019's policy despite no public signature change, since it's a behavior-level break.

---

## KI-044 — `resolve_contradiction()` can pick an already-`retracted` member as the winner, reactivating it to `active` ✓ RESOLVED (M3)

**Severity:** Architecture gap — a governed lifecycle action produces a semantically odd result (an `active` assertion with a closed `valid_to` window) for a state combination that didn't used to persist long enough to reach it
**Milestone target:** M3 — resolved via ADR-0031
**SPEC reference:** SPEC §10.3 (contradiction resolution — winner reactivation)

### Description

`resolve_contradiction(contradiction_id, winner_assertion_id, resolver)` gates winner eligibility purely on `winner_assertion_id in contradiction.member_ids` (SPEC §10.3) — it does not check the candidate's current `status`. Since KI-034 now keeps a `retracted` member's id in `Contradiction.member_ids` deliberately (that list is also `_reject_retract_if_party_to_contradiction`'s scan set, not audit-only), a resolver can pick that already-`retracted` member as the winner: its status flips to `active` (`ontology.py`, the `self.backend.set_assertion_status(winner_assertion_id, "active")` line), the value reappears in default query results, but `valid_to` stays closed at the original retraction time. That's not an impossible field combination on its own (an author-declared `valid_to` produces `active` + a closed window too) — what's wrong is that it silently undoes a *governance action's* close of that window (the retraction) with no new write recording that it reopened; `valid_to` still points at whatever closed it, now contradicted by `status=active`. Pre-existing (not introduced by KI-034), but KI-034 is what makes a `retracted` member persist inside an *open* contradiction long enough for a resolver to plausibly reach this path — before that fix, an extension would have already resurrected it back to `flagged`, an equally wrong but different outcome.

Found during KI-034's review (2026-08-05).

### Fix

ADR-0031: `resolve_contradiction()`'s winner-eligibility check now excludes members whose current status is `retracted` **or `superseded`**, raising `ValidationError` — the same exception type as the existing "winner not a member" check. The check runs only after the full party-to-contradiction loop has passed for every member (deterministic error precedence — a resolver who is both a party and picks a terminal-status winner always sees `CapabilityError`, never `ValidationError`, regardless of member iteration order), reusing the already-fetched winner object rather than a second lookup.

Review of the first version of this fix found `retracted`-only wasn't sufficient: `flag_contradiction()` already accepts a `superseded` assertion as a contradiction member by design (its own inline comment: "a flagged/superseded assertion must still be resolvable here"), and `_apply_with_conflict_routing`'s KI-034 extension-branch fix had only ever skipped re-flagging a `retracted` member, not a `superseded` one — so extending an open contradiction could flip a `superseded` member's status back to `flagged`, un-terminalizing it before `resolve_contradiction()`'s check ever saw it. Both gaps are closed together: the extension branch now skips `retracted` **and** `superseded` members, matching `flag_contradiction()`'s own already-correct handling.

Retraction/supersession is now terminal everywhere `resolve_contradiction()` and the write paths that decide a contradiction member's status are concerned — the winner check, the extension branch (KI-034's `retracted`-only fix widened to also cover `superseded`), and `flag_contradiction()`'s own already-both-aware re-flag guard. `resolve_contradiction()`'s own loser loop was one more site needing the same widening (found in the same review pass): it now skips a `superseded` loser the same way it already skipped a `retracted` one, instead of overwriting it to `retracted` and recording a second, resolver-misattributed event. **Not closed by this fix**, found in a later review pass and tracked separately: `retract()` itself can still overwrite a `superseded` contradiction member to `retracted`, bypassing the KI-033 party guard and KI-043 capability floor — see KI-051. A resolver who wants a terminal winner-candidate value active again has no reactivation path via `resolve_contradiction()` — they must submit it as a new assertion, which flows back through ordinary SPEC §10 conflict routing.

New conformance vectors in `conformance/test_contradiction_resolution.py` pin: picking an already-retracted or already-superseded member as winner raises `ValidationError`; the rejection runs before any write; picking a still-`flagged` member remains unaffected by the new check; (deterministic precedence) a resolver who is both a party and picks a terminal-status winner sees the party `CapabilityError`, not the terminal-status `ValidationError`; extending an open contradiction no longer resurrects a `superseded` member back to `flagged` (mutation-tested — the code fix alone left the suite green, so this vector was added specifically to close that gap); and no duplicate event for an already-superseded loser.

A contradiction whose every *existing* member ends up `retracted`/`superseded` has no eligible winner until a fresh assertion joins it — not fixed here, and not a permanent dead end either (a fresh assertion always resolves this, see below). The escape hatch is **predicate-shape-dependent, corrected during review**: for a `static` predicate, asserting the intended value again is enough (it rejoins the same contradiction as a new `flagged` member automatically). For a `time_varying` predicate — the only kind a `superseded` member's predicate can be — a bare re-assert does NOT rejoin the contradiction (it comes back `active` and untracked, since the extension shortcut is `static`-only); `flag_contradiction()` is the actual remedy there. Separately, `flag_contradiction()` can create a *brand-new* contradiction whose two founding members are already both terminal — not permanently unresolvable either (same fresh-assertion remedy applies), but starting with zero eligible winners is itself worth preventing at creation time — not addressed here, filed separately as KI-050. `retract()` itself bypassing the party/capability guards for a `superseded` member is a separate gap again, filed as KI-051.

**Breaking:** a resolver who was relying on picking an already-retracted or already-superseded member to reactivate it now gets `ValidationError` instead. Flagged in CHANGELOG per ADR-0019's policy despite no public signature change, matching KI-043's precedent for this class of behavior-level break.

---

## KI-045 — `flag_contradiction()` reads its target assertions and any existing open contradiction before opening its write transaction (TOCTOU)

**Severity:** Architecture gap — same bug shape as KI-035, on a method KI-035 didn't touch
**Milestone target:** Backlog
**SPEC reference:** SPEC §9.1 (proposal state machine); SPEC §10.3 (contradiction resolution)

### Description

`Ontology.flag_contradiction(assertion_id_a, assertion_id_b, author, rationale=None)` reads both target assertions and looks up any existing open contradiction for their `(subject, predicate)` — all before opening `with self.backend.transaction():`. Two concrete consequences of deciding from that stale snapshot:

- The KI-034 terminal-status guard (skip re-flagging a `retracted`/`superseded` assertion) tests a `status` read before the transaction. A concurrent `retract()` or supersession landing in the gap means the guard can still see a stale `active` status and re-flag an assertion that's since become terminal — the exact resurrection KI-034 closed, reachable via this race at only `propose` capability (including via MCP `ontolith.flag_contradiction`, which carries no write capability at all).
- The pre-fetched `existing` (open) contradiction is equally stale: a concurrent `resolve_contradiction()` that closes it in the gap leaves this call calling `update_contradiction_members()` on a now-`resolved` contradiction (no state guard on that write) and re-flagging the just-reactivated winner back into dispute.

Found during KI-035's review (2026-08-05) — KI-035 itself scopes to the four proposal-transition methods (`accept_proposal`/`reject_proposal`/`request_changes`/`resubmit`) and deliberately didn't expand to cover this separate method.

### Fix

Mechanically identical to KI-035's fix: move the assertion/contradiction reads and the terminal-status/existing-contradiction decisions to the first statements inside `flag_contradiction()`'s own `with self.backend.transaction():` block, re-reading fresh rather than trusting the pre-transaction snapshot. Add a conformance vector using the same `_RacingClock` technique KI-035 introduced (`conformance/test_review_workflow.py`).

---

## KI-046 — `DuckDBBackend` has no equivalent of `SQLiteBackend`'s concurrency lock (KI-023)

**Severity:** Architecture gap — backend-specific correctness gap; concurrency-dependent fixes (e.g. KI-035) are only airtight on SQLite today
**Milestone target:** Backlog
**SPEC reference:** Implementation Plan (conformance kit: both backends must satisfy the same guarantees)

### Description

`SQLiteBackend.begin()` (`store/sqlite/backend.py`) acquires a `threading.RLock` and holds it across the full span of an explicit transaction (KI-023) — a losing thread in a race blocks in `begin()` until the winner commits or rolls back, then re-reads and provably sees the winner's committed state. `DuckDBBackend.transaction()`/`begin()` (`store/duckdb/backend.py`) has no equivalent lock and no `_in_transaction` guard at all — two concurrent transitions on the same connection don't serialize the way SQLite's do; a second `BEGIN TRANSACTION` on the shared connection while one is already open is unguarded from the Python side.

Concrete consequence: KI-035's proposal-transition TOCTOU fix (and any future fix relying on the same "the transaction serializes concurrent callers" reasoning) is airtight for `SQLiteBackend` but not proven for `DuckDBBackend` — KI-035's own conformance vectors are parametrized over both backends via mocked/single-threaded racing (`_RacingClock`), which doesn't exercise real concurrency and so can't surface this gap either way.

Found during KI-035's review (2026-08-05).

### Fix

Decide (ADR) whether `DuckDBBackend` needs a KI-023-equivalent lock (simplest: mirror `SQLiteBackend`'s `threading.RLock` approach) or a different concurrency story (e.g. DuckDB's own multi-connection model, if `ontolith` ever moves away from one shared connection per backend instance). Add a real, threaded regression test for `DuckDBBackend` mirroring `tests/unit/test_sqlite_backend.py`'s KI-023 coverage once decided.

---

## KI-047 — `.trust_at_least()` ignores delegation attenuation (SPEC §8.4)

**Severity:** Architecture gap — a query-time trust check can disagree with the policy engine's own trust semantics for the same assertion
**Milestone target:** Backlog
**SPEC reference:** SPEC §8.4 (effective capability/trust under delegation)

### Description

`entities_meeting_trust` (`store/sqlite/backend.py`, `store/duckdb/backend.py`) joins `assertion.author = principal.id` and compares `principal.trust_level` directly. But an assertion made under delegation (`acting_as` set) has its *effective* trust attenuated — `govern/policy.py`'s `ThresholdPolicy`/`SourceQuorum` compute `min(principal.trust_level, acting_as.trust_level)` for exactly this reason (SPEC §8.4: "effective capability is min(author, acting_as) when delegating, not a wholesale substitution" — the same rule this project already applies to *capability*, `ontology.py:_check_direct_write_capability`). `entities_meeting_trust` reads only `author`'s raw `trust_level` and never looks at `acting_as` at all, so a low-trust delegate acting as a high-trust principal is scored as fully trusted by `.trust_at_least()` even though the policy engine that decided whether to auto-accept that same assertion would have scored it lower (or vice versa: a high-trust delegate acting as a low-trust principal is scored as low-trust by the query filter, though SPEC's `min()` rule agrees with that direction).

Pre-existing since `.trust_at_least()` first shipped; not introduced or worsened by KI-028 or KI-036, both of which touch how the underlying query is scoped/bitemporally filtered but neither of which reads `acting_as`. Found during KI-036's review while double-checking the "trust_level is exact, not an approximation" claim that fix's docstrings make — that claim is true for whether `trust_level` needs bitemporal reconstruction, but doesn't cover this separate gap in which column the query reads.

### Fix

`entities_meeting_trust` needs a `LEFT JOIN` to a second `principal` alias on `assertion.acting_as`, and to compare `min(p.trust_level, COALESCE(delegate.trust_level, p.trust_level))` (or equivalent `CASE`) against `min_trust`, matching `govern/policy.py`'s existing formula exactly. Needs a conformance vector with a delegated assertion where the author's and delegate's trust levels straddle the threshold in both directions, and a decision on whether `QueryBuilder`'s docstrings should say "effective trust_level" instead of "trust_level" once fixed.

---

## KI-048 — CLI has no `schema migrate` command

**Severity:** Architecture gap — SPEC-normative CLI surface remains partially unimplemented
**Milestone target:** Backlog
**SPEC reference:** SPEC §14.2 (`ontolith schema {show|migrate}`)

### Description

KI-038 added `ontolith schema show` but explicitly left `ontolith schema migrate` unimplemented, forward-tracked here per that entry's own Fix text ("split into its own issue if `show` lands first"). Schema versioning/migration isn't implemented anywhere in the codebase yet — `Ontology.apply_schema` enforces strict monotonic version numbering (`current_latest + 1`) but has no concept of a migration plan, diffing between versions, or data backfill; there is no `StorageBackend` port method or SDK call for anything migration-shaped, on any interface (SDK, REST, MCP, CLI).

### Fix

Needs a design decision before implementation, not just a CLI command: what does "migrate" mean here — applying a new `SchemaIR` version is already possible via `apply_schema`, so `ontolith schema migrate` most plausibly means either (a) a thin CLI wrapper around `apply_schema` reading a YAML/class-DSL file from disk (the smallest useful slice, no new domain logic), or (b) something that also handles property renames/type changes against existing assertion data (a substantially larger scope touching append-only semantics — renaming a predicate doesn't rewrite historical assertions, so old and new predicate names would coexist, which needs its own ADR). Scope this to (a) first if picked up, and record the (a)/(b) boundary decision as an ADR rather than deciding it implicitly inside a CLI PR.

---

## KI-049 — A predicate's declared `value_type` token is checked at write time (KI-031), but the literal's actual string content is never validated to match it

**Severity:** Architecture gap — declared schema constraints can silently diverge from stored data
**Milestone target:** Backlog
**SPEC reference:** SPEC §4 (`value_type` is a core type); Implementation Plan §4.3 ("validate at edges")

### Description

KI-031 made `_require_known_predicate` (`src/ontolith/ontology.py`) reject a literal write whose caller-supplied `value_type` *token* doesn't match the schema's declared `value_type` for that predicate (e.g. writing `value_type="Text"` against a predicate declared `Integer`). It does not — and never has — validated that `value` itself is actually well-formed for that type. `kb.assert_literal(entity.id, "Person.age", "unknown", "Integer", author)` succeeds today: `value_type="Integer"` matches the schema's declaration, so the token check passes, even though `"unknown"` is not a valid integer. The same gap exists for `Date`/`DateTime`/`URI`/`JSON` — nothing parses `value` against its claimed format.

Found during KI-039's review: `.where()`'s new `__gt`/`__lt`/`__gte`/`__lte` range operators trust a predicate's declared `value_type` (via `SchemaIR.value_type_of()`) to decide whether `CAST(value_lit AS REAL/DOUBLE)` is safe, but "declared numeric" and "actually stored as parseable numeric text" are different guarantees — this gap is what lets them diverge. Also reachable via schema evolution: a property declared `Text` in schema v1, retyped to `Integer` in v2 (nothing re-validates or migrates existing rows written under v1 — see KI-048's own note that no migration/backfill mechanism exists at all).

### Fix

`_require_known_predicate` (or a new validator called from the same write paths: `assert_literal`, `propose`, `_replay_proposal_operations`) needs to parse `value` against the schema-declared `value_type` and reject on mismatch — `int()`/`float()` for `Integer`/`Float`, `datetime.fromisoformat()` (or equivalent) for `Date`/`DateTime`, a URI parser for `URI`, `json.loads()` for `JSON`. Needs a decision on `Boolean` (accept `"true"`/`"false"` case-insensitively? `"1"`/`"0"`?) and on whether this applies retroactively to already-stored data (it can't, without a migration mechanism — KI-048) or only to new writes going forward. Conformance vectors should cover each `value_type`'s accept/reject boundary, plus confirm `.where()`'s range operators (KI-039) correctly exclude/behave once this closes the gap that currently makes `TRY_CAST`/silent-zero-coercion necessary as a defensive fallback in `entities_where()`.

---

## KI-050 — `flag_contradiction()` can create a contradiction whose two founding members are already both terminal, with no eligible winner among them

**Severity:** Architecture gap — a `propose`-capability action can open a `Contradiction` `resolve_contradiction()` can't immediately close, requiring a follow-up write before it's resolvable at all
**Milestone target:** Backlog
**SPEC reference:** SPEC §10.3 (contradiction resolution — winner reactivation), §14 (MCP `ontolith.flag_contradiction`)

### Description

`flag_contradiction(assertion_id_a, assertion_id_b, author)` validates that the two named assertions share a subject and predicate, and — since KI-034/KI-044 (ADR-0031) — deliberately accepts either (or both) already being `retracted`/`superseded`, so it can name a terminal assertion when extending an open contradiction. Nothing stops both of the two assertions passed at *creation* time from already being terminal: `flag_contradiction(retracted_id, superseded_id, author)` opens a brand-new `Contradiction` whose two founding `member_ids` are both ineligible winners under ADR-0031's check — `resolve_contradiction()` fails against every existing member immediately.

**Correction (found in this KI's own third review pass, 2026-08-21): the contradiction is not permanently unresolvable, and an earlier draft of this entry — and of ADR-0031's Consequences — said so incorrectly.** A third assertion for the same `(subject, predicate)` reaches it exactly as ADR-0031 describes for any all-terminal contradiction: for a `static` predicate, a plain `propose()`/`assert_literal` extends the same open contradiction automatically; for a `time_varying` predicate (reachable here too — `flag_contradiction()` itself is not restricted to `static` predicates the way auto-detected, conflict-routing-opened contradictions are, SPEC §10.1), a second `flag_contradiction()` call naming the fresh assertion alongside an existing member is the remedy, per ADR-0031. The real gap is narrower: `flag_contradiction()` lets a caller open a contradiction that starts with *zero* eligible winners, forcing that follow-up write before `resolve_contradiction()` can ever succeed — a state a *caller* has to know to work around, not a state nothing can recover from.

Reachable at only `propose` capability, including by an AI principal over MCP (`ontolith.flag_contradiction` carries no write capability at all) — a low-privilege or automated caller can create a contradiction with no immediately eligible winner, with no elevated action required.

Found during KI-044/ADR-0031's second review pass (2026-08-19), while confirming ADR-0031's escape-hatch claim; this entry's own overclaim (see Correction above) found during the third review pass (2026-08-21).

### Fix

Decide (ADR) whether `flag_contradiction()` should require *at least one* of the two named assertions to be currently `active` or `flagged` (i.e., not already terminal) at creation time — raising `ValidationError` otherwise, mirroring how `resolve_contradiction()` now validates winner eligibility. Extending an *already-open* contradiction with an all-terminal pair should very likely remain permitted (that's the case ADR-0031's own design deliberately supports — naming a terminal assertion for audit/context when extending); the gap is specifically about a *brand-new* contradiction's two founding members both being terminal already. Add a conformance vector pinning whichever behavior is chosen.

---

## KI-051 — `retract()` can overwrite an already-`superseded` contradiction member to `retracted`, bypassing both the party guard and the capability floor

**Severity:** Architecture gap — a governance action (KI-033's self-dealing guard, KI-043's capability floor) can be bypassed for one specific terminal status it wasn't written to recognize
**Milestone target:** Backlog
**SPEC reference:** SPEC §10.3 (contradiction resolution), §5 (append-only lifecycle)

### Description

`_reject_retract_if_party_to_contradiction` (KI-033) and `_open_contradiction_if_flagged_member`/`_meets_retract_contradiction_floor` (KI-043, ADR-0030) both early-return — treating the retraction as ordinary, ungoverned — whenever the target assertion's status is anything other than `flagged`. That was a safe simplification before ADR-0031: a contradiction member could only ever be `flagged` or `retracted`, and a `retracted` target is already excluded from re-retraction by `retract()`'s own idempotency elsewhere. ADR-0031 (KI-044) changes the picture: `superseded` is now a designed, documented status a contradiction member can hold while its contradiction is still `open` (via `flag_contradiction()`, which accepts a `superseded` assertion by design). Neither guard was updated to recognize it — both still only check `status == "flagged"`/`status != "flagged"` framed around the pre-ADR-0031 assumption.

Verified: a principal who is a party to the contradiction (author of one of the disputed values), with only `write` capability, calls `retract()` on a `superseded` member of an *open* contradiction — it auto-accepts directly, overwriting `status` from `superseded` to `retracted` with no party check and no review/admin floor ever applied. This does not reopen KI-044 itself (`retracted` and `superseded` are both terminal, so `resolve_contradiction()`'s winner check still correctly rejects the result either way), and `_retraction_valid_to` correctly refuses to widen the already-closed validity window, so no temporal corruption results. The harm is attribution and governance bypass: `status` stops recording *how* the assertion actually became terminal (supersession vs. explicit retraction), and a party to the dispute — exactly who KI-033 exists to block — can cause that overwrite at a capability floor (`write`) below what KI-043 requires for a `flagged` member of the same contradiction.

Found during KI-044/ADR-0031's third review pass (2026-08-21).

### Fix

Decide (ADR) whether `_reject_retract_if_party_to_contradiction` and the KI-043 capability-floor check should treat `superseded` the same as `flagged` for guard-applicability purposes (i.e. "this target belongs to an open contradiction and is governed by these guards" should key off contradiction membership, not off `status == "flagged"` specifically) — most likely by changing `_open_contradiction_if_flagged_member`'s underlying membership check (and the equivalent inline check in `_reject_retract_if_party_to_contradiction`) to recognize a `superseded` member of an open contradiction as still governed, not just a `flagged` one. Add conformance vectors pinning both guards now apply to a `superseded` member the way they already do to a `flagged` one.

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
