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

Compounding this: `Ontology.proposals()` defaults to `state="require_review"` (`src/ontolith/ontology.py:1363`) — the call a reviewer would naturally make to see "what's pending" — which silently excludes `changes_requested` proposals. A reviewer must already know to pass `state="changes_requested"` or `state=None` to ever see a proposal again after requesting changes on it.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

Added `Ontology.resubmit(proposal_id, author)` implementing SPEC §9.1's `changes_requested → submitted → {policy}` transition: only the proposal's own author or delegate may call it (the inverse of `_require_reviewer`'s self-review guard); the existing payload is replayed unedited through a fresh policy evaluation, exactly as a first submission via `propose`/`propose_ref`. `Ontology.accept_proposal`'s operation-replay loop was extracted into a shared `_replay_proposal_operations` helper so `resubmit`'s auto-accept branch doesn't duplicate it. `_finalize_non_accepted_decision` (shared with `propose`/`propose_ref`/`retract`) gained an `is_new` flag: `resubmit` re-decides an *existing* persisted row via `update_proposal_state`, where the original callers `INSERT` a brand-new one via `put_proposal` — reusing the insert path on an existing id raised a UNIQUE-constraint `StorageError`, caught by the conformance suite before this shipped. No `ProposalEvent` is recorded for the resubmission itself (an author action, not one of SPEC §9.4's five reviewer actions — `propose`'s own auto-accept path likewise records none). REST gained `POST /proposals/{id}/resubmit`; CLI/MCP parity is deferred to KI-032 (CLI proposal-review commands).

`Ontology.proposals()`'s default changed from `state="require_review"` to `state="pending"`, a new query-level alias merging `require_review` and `changes_requested` — both are still-open proposals needing someone's attention, and without the merge a `changes_requested` proposal remained invisible to the default review-queue query even after `resubmit` existed to act on it. Passing a state explicitly (e.g. `state="require_review"`) still returns a single, unmerged state. `GET /proposals` and `ontolith proposal list --state` both default to `"pending"` for the same reason.

---

## KI-028 — `.min_confidence()`/`.trust_at_least()` reintroduce an N+1 backend-round-trip pattern

**Severity:** Performance — unbounded per-entity (and per-assertion) backend round trips, unbenchmarked
**Milestone target:** M3
**SPEC reference:** Implementation Plan §9 (performance budgets)

### Description

`QueryBuilder._apply_confidence_trust_filters` (`src/ontolith/query/builder.py:216-235`) runs after `_base_candidates()` resolves the full pre-filter entity list — unbounded when neither `.where()` nor `.semantic()` is chained (`self._backend.entities(namespace=, concept=)` returns every entity of the concept). For each candidate, `_passes_confidence_trust` issues a separate `backend.assertions(subject=entity.id, status="active")` call, and `.trust_at_least()` additionally calls `backend.get_principal(author_id)` once per assertion, uncached even across repeated authors within the same query. `kb.query("Person").trust_at_least(5)` with no other filter on a 100k-entity concept issues on the order of 100k+ separate backend calls before `.limit()` is ever applied.

This is the same defect class KI-001 was created and fixed for — reintroduced, apparently unnoticed, when `.min_confidence()`/`.trust_at_least()` shipped alongside `.semantic()` as part of KI-018's hybrid-retrieval resolution. `tests/benchmarks/test_hybrid_query.py` exercises `.semantic()` and `.semantic()+.where()` only; neither confidence nor trust filtering is benchmarked, so the regression is invisible to CI.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

Push `.min_confidence()`/`.trust_at_least()` into a SQL `EXISTS` subquery per filter, mirroring `entities_where()`'s existing per-predicate subquery pattern (both backends), instead of the current Python-side per-entity loop. Add a benchmark exercising both filters at realistic scale (mirroring `test_hybrid_query.py`'s existing large-KB fixture).

---

## KI-029 — MCP `ontolith.schema` and REST `GET /schema` omit `relations`

**Severity:** Architecture gap — SPEC-required schema information is unreachable via two of the four primary interfaces
**Milestone target:** M3
**SPEC reference:** SPEC §14.4 (`ontolith.schema` MUST "Return concepts/relations/temporality")

### Description

`schema_tool` (`src/ontolith/interfaces/mcp.py:64-103`) iterates `concept_def.properties` only; `ConceptDef.relations` (`src/ontolith/schema/ir.py:58-73`, populated for every schema that declares a `Relation`) is never read. `GET /schema`'s `ConceptOut`/`get_schema` route (`src/ontolith/interfaces/rest.py:98-103`, `:495-...`) has the identical gap — no relations field on the response model at all.

An agent or REST client inspecting the schema this way cannot see that a relation like `Person.employer` exists, or whether it's `time_varying` — exactly the information that predicts whether a subsequent proposal on that predicate will supersede or contradict (SPEC §10.1).

Surfaced during a whole-project milestone audit (2026-07-29); flagged in a prior 2026-07-14 audit and not yet closed.

### Fix

Add a `relations` list (target concept, inverse, cardinality, temporality) alongside `properties` in both `schema_tool`'s dict output and REST's `ConceptOut`/`SchemaOut` models.

---

## KI-030 — `QueryBuilder.where()` silently no-ops on relation-traversal filter keys

**Severity:** Test gap / DX — a documented example produces an empty result with no error
**Milestone target:** M3
**SPEC reference:** N/A — internal DX/correctness gap, not a SPEC deviation

### Description

`QueryBuilder`'s class docstring (`src/ontolith/query/builder.py:28-31`) advertises `kb.as_of("2025-01-01").query(Person).where(employer__name="Acme Corp")` as a working example; `.where()`'s own docstring (`:63-67`) still says "M2 will add relation traversal." `_qualified_filters()` (`:166-168`) compiles any keyword into the literal predicate string `f"{concept}.{key}"` — `employer__name` becomes the literal predicate `"Person.employer__name"`, which matches no assertion. Even a correctly-named relation-target filter (e.g. `employer="org-id"`) can't match, since `entities_where()`'s SQL (`store/{sqlite,duckdb}/backend.py`) only compares `value_lit`, never `value_ref`. `.where()` accepts any of this silently and returns an empty list — no error, no warning — for a pattern its own docstring calls working, two milestones after the "M2 will add" comment was written.

Surfaced during a whole-project milestone audit (2026-07-29); flagged in a prior 2026-07-14 audit and not yet closed.

### Fix

Reject unknown/dunder-containing filter keys with `ValidationError` at `.where()` call time rather than silently compiling them into an unreachable predicate string. Correct the class docstring's example and `.where()`'s own docstring to the actual symbolic-equality-only contract, or record an ADR if relation traversal is being deliberately deferred rather than simply unbuilt.

---

## KI-031 — Schema `value_type`/`required` are declared but never enforced at write time

**Severity:** Architecture gap — declared schema constraints are silently unenforced
**Milestone target:** M3
**SPEC reference:** SPEC §4 (constraints are validator-backed); Implementation Plan §4.3 ("validate at edges")

### Description

`Ontology._require_known_predicate` (`src/ontolith/ontology.py:397-409`) rejects an unknown predicate at write time but never checks the caller-supplied `value_type` against the schema-declared `PropertyDef.value_type` (`src/ontolith/schema/ir.py:12-27`), and nothing checks `PropertyDef.required`/`RelationDef.required` at all. A predicate declared `value_type: Integer` in schema currently accepts `assert_literal(..., value_type="Text", ...)` without error; a `required: true` property is never checked as present on an entity.

A prior audit (2026-07-14) flagged this as partially open; cardinality is now enforced (`govern/conflict.py`, ADR-0017) and unknown predicates are now rejected, but the `value_type`/`required` remainder was never itself tracked as its own entry.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

Validate `value_type` against the schema-declared type in `_require_known_predicate` (or a sibling helper), raising `ValidationError` on mismatch. Decide and record (ADR) whether/how `required` is enforced at the core layer, given `RequiredFieldsValidator` already exists as a plugin-level alternative (KI-010) — the core-vs-plugin division of responsibility here needs to be an explicit decision, not silence.

---

## KI-032 — CLI has no `proposal accept`/`reject`/`review` commands

**Severity:** Test gap / DX — one of SPEC's four primary interfaces cannot act on its own review queue
**Milestone target:** M3
**SPEC reference:** SPEC §14.2 (CLI command surface, `proposal {list|review}`)

### Description

`src/ontolith/interfaces/cli.py`'s `proposal_app` has exactly one subcommand, `list` (`:347`). `Ontology.accept_proposal`/`reject_proposal`/`request_changes` all exist, and REST exposes all three (`POST /proposals/{id}/accept|reject|review`), but the CLI — one of SPEC's four primary interfaces alongside SDK/REST/MCP — has no way to act on any of them. An operator using only the CLI can discover what's pending review (`proposal list`) but cannot accept, reject, or request changes on any of it.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

Add `ontolith proposal accept <id> --author`, `proposal reject <id> --author [--reason]`, and `proposal review <id> --author [--reason]` commands, mirroring the existing `principal issue-token`/`revoke-token` command shape (an `--author` option identifying the reviewer, calling straight through to the corresponding `Ontology` method).

---

## KI-033 — `retract()` lets a party to an open contradiction unilaterally retract the opposing member

**Severity:** Architecture gap — same governance outcome as KI-026, reachable through a different method that has no contradiction awareness at all
**Milestone target:** Backlog
**SPEC reference:** SPEC §10.3 (contradiction resolution)

### Description

`Ontology.retract()` routes through `self.policy` like any other governed write; a human principal with `review` (or higher) capability auto-accepts under `ThresholdPolicy`. `retract()` performs no check on whether the target assertion is a `flagged` member of an open contradiction, nor whether the caller is a party to that contradiction (author/delegate of any of its members). A principal who authored one side of a disputed static fact can therefore retract the *opposing* member directly — reproduced: after such a retraction, the contradiction is left `open` with only the retracting party's own value still `flagged`, so a later legitimate resolver effectively has no real choice left to make.

KI-026 closed the front door (`resolve_contradiction` itself); this is a side door reaching a similar outcome through `retract()`, which has no notion of contradictions at all today. Found while fixing KI-026, not introduced by it — pre-existing.

### Fix

Reject retraction of an assertion that is currently a `flagged` member of an open contradiction when the retracting principal is a party to that contradiction (author/delegate of any member) — mirroring KI-026's "any member, not just one side" reasoning. Needs a new conformance vector suite; likely requires `retract()` to look up whether the target assertion belongs to an open contradiction before evaluating policy, which it currently never does at all.

---

## KI-034 — Extending an open contradiction can resurrect an already-`retracted` member back to `flagged`

**Severity:** Test gap — a documented lifecycle transition (`retracted` is meant to be terminal) doesn't hold under a specific sequence
**Milestone target:** Backlog
**SPEC reference:** SPEC §5 (assertion lifecycle — `retracted` status)

### Description

Reproduced while investigating KI-033: if an assertion belonging to an open contradiction is retracted (e.g. via the KI-033 gap, or by any other means reaching `retract()`), a subsequent `propose()`/`assert_literal` on the same `(subject, predicate)` that extends the same open contradiction can flip that already-`retracted` assertion's status back to `flagged`. `retracted` is otherwise treated as a terminal status everywhere else in the codebase (SPEC §5's append-only lifecycle); this is the one path found so far where it isn't.

### Fix

Needs a design decision, not just a code fix: should conflict routing (`govern/conflict.py`) exclude `retracted` assertions from the set of "existing members" it can add to when extending a contradiction, treating a retraction as final regardless of the contradiction's own open/resolved state? Record as an ADR update once decided; add a conformance vector pinning the corrected behavior.

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
