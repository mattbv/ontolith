# ADR-0046: `Proposal.reviewers` Persisted at Creation, `assign_reviewers()` for SPEC §9.4's `assign` Action

**Status**: Accepted

**Date**: 2026-09-05

**Deciders**: Ontolith Core Team

**Related**: SPEC §9.2 (policy engine contract — `RequireReview.reviewers`), SPEC §9.4 (review
workflow — names `assign` as one of five MUST-recorded review actions), ADR-0045
(`RequireReviewByRole` — the strategy whose entire purpose this ADR makes actionable), ADR-0022
(its own Update section records `resubmit`/KI-027 — the re-evaluation path this ADR's reviewer
refresh behavior extends), ADR-0042 (Admin-Action Audit Trail — the closest existing precedent for
adding a persistence column plus a migration step for an
existing table), `docs/known-issues.md` KI-078 (closed by this ADR)

---

## Context

Every `PolicyStrategy` that returns `RequireReview(reviewers, reason)` computes a reviewer list,
but nothing downstream ever stored or exposed it: `Proposal` had no `reviewers` field,
`Ontology`'s non-auto-accept path (`_finalize_non_accepted_decision`) persisted only
`policy_reason`, and no `assign` review action existed anywhere in `src/`, despite SPEC §9.4
naming it as one of five review actions the spec says MUST be recorded (`assign`, `comment`,
`accept`, `reject`, `request_changes` — only the latter three existed as `Ontology` methods before
this ADR). This had been true since `ThresholdPolicy`'s own `reviewers=[]` default shipped in M1,
but stayed low-consequence because every strategy's reviewer list was either always empty
(`ThresholdPolicy`'s default case) or a secondary detail alongside a decision that mattered more on
its own (`SourceQuorum`).

KI-069's `RequireReviewByRole` (ADR-0045) made this gap materially worse: its entire stated
purpose is choosing which reviewers a proposal routes to, not deciding whether review is needed at
all (it never auto-accepts). Configuring it produced a `Decision.reviewers` value only a direct SDK
caller inspecting the returned object ever saw — REST/MCP/CLI callers saw only the free-text
`policy_reason`, a string a caller would have to parse, not a queryable assignment. KI-078 was
filed during that review to track closing this gap.

## Decision

**Two additive pieces, closing both halves of the gap:**

**1. `Proposal.reviewers: list[str]`** — a new field, populated from `RequireReview.reviewers` at
proposal-creation time (`Ontology._finalize_non_accepted_decision`) and refreshed whenever
`resubmit` (KI-027) re-evaluates policy and lands back in `require_review` (the freshly-computed
reviewers may differ from the original assignment — refreshed, not left stale). Not cleared on
accept/reject/request_changes — it remains a historical record after a decision is made, the same
way `policy_reason` does.

**2. `Ontology.assign_reviewers(proposal_id, reviewers, actor)`** — implements SPEC §9.4's `assign`
action. Replaces the reviewer list wholesale (not merged — a caller passing a partial update must
include the names it wants to keep), records a `ProposalEvent(type="assign")`, and reuses the
*exact* eligibility checks `accept_proposal`/`reject_proposal`/`request_changes` already share
(`_require_reviewer_principal`/`_require_pending_proposal`): `actor` must hold `review`/`admin`
capability, be non-AI, and not be the proposal's own author or delegate. Exposed via REST as
`POST /proposals/{proposal_id}/assign` — REST alone, narrower than ADR-0042's own KI-072 update
(which shipped both REST and CLI, on the reasoning that an operator investigating "who did this"
is a CLI-first workflow); `assign` has no equivalent motivating audit-investigation use case, so
CLI/GraphQL/MCP are all deferred here rather than just GraphQL/MCP (see Consequences).

### Storage: a new column, not a `ProposalEvent`-derived value

`reviewers` is a plain, directly-queryable column on `proposal` (JSON-encoded, mirroring
`payload`/`metadata`'s own convention) — not something a reader has to reconstruct by replaying
`proposal_event` rows. This mirrors `policy_reason`'s own existing shape rather than introducing a
new "current state lives in the event log" pattern this codebase doesn't otherwise use for
proposals. `put_proposal`/`get_proposal` needed no signature change (the whole `Proposal` object,
`reviewers` included, already flows through them); a new `StorageBackend.update_proposal_reviewers`
port method was added for the two cases where an already-persisted row's reviewers need updating
independently of `state` (resubmit's refresh, and `assign_reviewers` itself) — not folded into
`update_proposal_state`, since `assign` doesn't change `state` at all and that method's signature
already carries enough state/decided_at/policy_reason-specific semantics without a fourth,
differently-shaped parameter.

Both backends needed a schema migration for existing database files (`CREATE TABLE IF NOT EXISTS`
is a no-op against one that already has the table) — the same pattern ADR-0042 established for
`principal_credential.issued_by`/`revoked_by`. **DuckDB's migration path differs from SQLite's**:
DuckDB's `ALTER TABLE ADD COLUMN` rejects any constraint ("Adding columns with constraints not yet
supported"), so the migrated column is added nullable and explicitly backfilled with `'[]'`
afterward, while a freshly-created database's own `CREATE TABLE` still gets the real
`NOT NULL DEFAULT '[]'` constraint. SQLite's `ALTER TABLE ADD COLUMN` has no such restriction and
uses the same `NOT NULL DEFAULT '[]'` in both paths.

### Why `assign_reviewers` reuses the self-review guard

An author who could freely pick their own proposal's reviewer set would undermine
`_require_pending_proposal`'s guard just as much as picking their own accept/reject outcome would
— routing your own proposal to a reviewer you expect to rubber-stamp is a real way to defeat human
review, not a hypothetical one. Reusing the identical check `accept_proposal`/`reject_proposal`/
`request_changes` already share, rather than inventing a looser rule for `assign` specifically,
keeps the guard's actual security property intact.

## Consequences

**Positive:**
- Closes KI-078: both halves of the gap (persisting a strategy's own computed reviewers, and a way
  to change them later) are closed in the same PR, matching how the KI's own Fix text described
  them as one cohesive ask.
- `RequireReviewByRole` (ADR-0045) is now actually actionable through REST, not just visible to a
  direct SDK caller.
- Reuses every existing eligibility-check helper and audit-trail mechanism
  (`_require_reviewer_principal`, `_require_pending_proposal`, `ProposalEvent`) rather than
  inventing new ones — `assign` is a fourth call alongside three that already existed, not a new
  pattern.

**Negative / follow-ups:**
- **Breaking**: `StorageBackend` gains a new required `update_proposal_reviewers()` method — any
  third-party backend implementation must add it.
- GraphQL/CLI/MCP are not updated to surface `reviewers` or expose an `assign` action — REST only.
  A deployment using any of those three interfaces still can't read or change reviewer assignments
  through them yet; this is deliberate scope, not an oversight, and should be picked up if/when a
  consumer of those interfaces needs it (the same reasoning ADR-0042's own Update section used for
  its admin-event read surface, though that update went further and included CLI too — see
  Alternatives below for why this ADR doesn't).
- `reviewers` is plain free-text strings (principal IDs), unvalidated against the `principal` table
  at write time — `assign_reviewers` doesn't check that each name in the list is a real,
  review-capable principal. This matches `ThresholdPolicy`'s own pre-existing behavior (its
  `reviewers=[principal.owner]` was never validated either) rather than introducing a new
  validation gap; tightening it is separable future work if it matters for a deployment.
- DuckDB's migrated `reviewers` column lacks the `NOT NULL` constraint a fresh database's column
  has (see Decision above) — application code (`_row_to_proposal`) treats a `NULL` defensively, so
  this is not a correctness gap, but it is a real, documented asymmetry between a migrated and a
  fresh DuckDB file that a future schema audit should know about.

## Alternatives Considered

- **Store reviewers only as `ProposalEvent` details, deriving "current reviewers" by replaying the
  latest `assign` event (or falling back to the original policy decision):** rejected — this would
  introduce an "authoritative state lives in the event log" pattern this codebase doesn't otherwise
  use for proposals (`policy_reason`/`state` are both plain columns), and would need extra logic
  everywhere `Proposal.reviewers` is read to know whether to trust the column or replay events.
  Mirroring `policy_reason`'s existing shape is simpler and consistent.
- **Fold reviewer updates into `update_proposal_state`** via a new optional parameter: rejected —
  `assign` doesn't touch `state`, and that method's signature already juggles `state`/`decided_at`/
  `policy_reason` with three different "does None mean leave-unchanged or clear" semantics; a
  fourth, differently-shaped parameter would make an already-dense signature worse for a use case
  (updating reviewers alone) that doesn't need `state` touched at all.
- **A looser eligibility check for `assign` than accept/reject/request_changes** (e.g., allowing the
  proposal's own author to self-assign reviewers, on the theory that routing isn't a decision):
  rejected — see the Decision section's own rationale; self-assignment is a real way to defeat
  human review, not a hypothetical one.
- **Expose `assign` through every interface (REST, GraphQL, CLI, MCP) in this same PR:** rejected —
  REST is the natural first target (the same reasoning ADR-0042's KI-072 update used); speculatively
  wiring three more surfaces before any consumer needs them is scope beyond what closing KI-078
  required.
- **Also ship the CLI surface, matching ADR-0042's KI-072 update exactly:** rejected — KI-072 added
  CLI specifically because "who did this" admin-event auditing is a CLI-first workflow for an
  operator; `assign` has no equivalent motivating scenario tying it to the CLI specifically, so
  there's no comparable reason to prioritize it over GraphQL/MCP here. Left for whichever interface
  a real consumer needs first, not assumed to be CLI by default.

## References

- SPEC §9.2: Policy engine contract (`RequireReview.reviewers`)
- SPEC §9.4: Review workflow (`assign` action)
- ADR-0045: `RequireReviewByRole` and the other KI-069 strategies (the motivating consumer)
- ADR-0042: Admin-Action Audit Trail (the schema-migration-for-an-existing-table precedent this ADR
  follows, and the "REST first, other interfaces deferred" precedent it also follows)
- ADR-0022's own Update section / KI-027: `resubmit` (the re-evaluation path whose reviewers now refresh)
- `docs/known-issues.md` KI-078 (resolved by this ADR)
