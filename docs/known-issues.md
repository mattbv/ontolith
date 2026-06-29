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

## KI-003 — Append-only conformance vector does not directly test frozen model

**Severity:** Test gap — conformance coverage  
**Milestone target:** M2 (low priority)  
**SPEC reference:** SPEC §5 (append-only assertions)

### Description

`conformance/test_append_only.py::test_value_field_is_immutable` demonstrates that new values create new assertion records rather than overwriting existing ones — but it never directly asserts that attempting to mutate `assertion.value` in-place raises an error.

`Assertion` is correctly `frozen=True` (via `model_config = ConfigDict(frozen=True)` in `src/ontolith/core/assertion.py`), so in-place mutation does raise `ValidationError`. The conformance vector just doesn't prove it.

### Fix

Add a test case that does:
```python
with pytest.raises(ValidationError):
    assertion.value = "tampered"
```
This can be added to `conformance/test_append_only.py` or to the property tests.

---

## KI-004 — Symbolic query filter covers only `retracted` status in append-only vector

**Severity:** Test gap — partial conformance coverage  
**Milestone target:** M2 (with conflict/supersession vectors)  
**SPEC reference:** SPEC §5, §10

### Description

`conformance/test_append_only.py` tests that retracted assertions are excluded from default queries and retained in history. It does not cover the `superseded` and `flagged` statuses, which also must be excluded from default retrieval (SPEC §5) and queryable for audit.

These statuses only arise from the conflict-routing logic (SPEC §10 — supersession and contradiction), which is a M2 deliverable. The vectors covering those paths belong with the M2 conflict conformance suite.

### Fix

In M2, add conformance vectors to `conformance/test_conflict.py` that verify:
- Superseded assertions excluded from default queries, retained in history
- Flagged assertions excluded from default queries, queryable with `status="flagged"`

---

## KI-005 — `assert_literal` / `assert_ref` bypass the proposal/policy path ✓ RESOLVED (M2)

**Severity:** Architecture gap — not SPEC-compliant for untrusted principals  
**Milestone target:** M2 — resolved in `feat(govern): implement proposal workflow, conflict routing, and contradiction handling`  
**SPEC reference:** SPEC §9 ("all writes through proposal path")

### Description

`Ontology.assert_literal()` and `Ontology.assert_ref()` write assertions directly to the backend without creating a `Proposal` or running `ThresholdPolicy`. This means:

- AI principals can write assertions without owner review
- Low-capability principals (e.g., `propose`) bypass policy
- The audit trail has no proposal record

The M1 CLI and quickstart use these methods for simplicity. The policy engine (`ThresholdPolicy`) exists and is tested in isolation, but is not wired into the write path.

### Fix

In M2, add `Ontology.propose(...)` that creates a `Proposal`, evaluates it through the configured `PolicyStrategy`, and — if `AutoAccept` — commits the assertions in a single transaction (SPEC §9, ADR-0010). Rename or deprecate `assert_literal` / `assert_ref` as convenience wrappers that call `propose()` internally.

---

## KI-006 — `govern/policy.py` `Reject` decision is never produced

**Severity:** Informational — dead code  
**Milestone target:** M2  
**SPEC reference:** SPEC §9.2

### Description

`ThresholdPolicy.evaluate()` can return `AutoAccept` or `RequireReview` but never `Reject`. The `Reject` decision class exists but has no production path. Line 45 in `govern/policy.py` (`return Reject(...)`) is unreachable from `ThresholdPolicy`, leaving it uncovered.

### Fix

When the proposal workflow is wired in M2 (`KI-005`), add a rejection rule — for example, rejecting proposals from principals with trust_level 0 or `read` capability — that exercises the `Reject` path.

---

## KI-007 — No API to accept or reject a `require_review` proposal

**Severity:** Architecture gap — pending proposals are unresolvable  
**Milestone target:** M2 (review workflow)  
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
