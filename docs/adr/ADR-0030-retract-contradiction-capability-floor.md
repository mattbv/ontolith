# ADR-0030: `retract()` Routes to Review, Instead of Auto-Accepting, When It Can't Meet `resolve_contradiction()`'s Capability Floor

**Status**: Accepted
**Date**: 2026-08-18
**Deciders**: Ontolith Core Team
**Related**: SPEC §10.3 (contradiction resolution), ADR-0003 (AI proposals always require review), KI-026 (self-resolution guard), KI-033 (retract party-to-contradiction guard), KI-034 (retracted stays terminal across extension), KI-043

---

## Context

`resolve_contradiction()` requires `review`/`admin` capability and blocks AI-kind resolvers (SPEC §10.3: a disputed static fact is adjudicated by review, not auto-resolved). `retract()` has no such floor — any human/service principal with plain `write` capability auto-accepts under `ThresholdPolicy` for any retraction, including one targeting a `flagged` member of an open contradiction.

KI-033 closed the *self-dealing* half of this gap: a party to the contradiction (author or delegate of either disputed value) can no longer retract a member via `_reject_retract_if_party_to_contradiction`. But that guard is about *who*, not *how capable* — a **neutral** `write`-capability principal, party to neither disputed value, could still retract one member outright. The effect is close to unilaterally adjudicating the dispute (the contradiction ends up with one member gone and the other implicitly "winning" by being the only one left), reachable at a materially lower capability floor than `resolve_contradiction()` requires for the equivalent action. `conformance/test_contradiction_resolution.py::TestRetractContradictionGuard::test_neutral_third_party_can_still_retract_flagged_member` pinned this as current, intentional-for-now behavior — surfaced during KI-033's own review as a narrower guard than `resolve_contradiction()`'s, with nothing stating the gap was deliberate.

## Decision

Retracting a `flagged` member of an *open* contradiction now requires the same floor `resolve_contradiction()` enforces: `review` or `admin` capability, and the acting principal must not be AI-kind. An ordinary retraction (target not currently a flagged member of an open contradiction) is unaffected — still just `write`.

**A principal below that floor is routed to review, not rejected outright.** If `assertion_id` is currently a flagged member of an open contradiction and the retracting principal doesn't meet the floor, `retract()` (and `resubmit()`, on its own re-evaluation) substitutes a `RequireReview` decision for whatever `self.policy` would otherwise have decided, *before* ever evaluating policy or opening a write transaction. A `review`-capable, non-AI principal can then accept the resulting proposal via `accept_proposal()` — exactly the same review queue every other under-capability proposal already uses. This mirrors how an AI-authored proposal already always requires review under `ThresholdPolicy` rather than being rejected: capability shortfalls route to review, they don't dead-end.

Three new small `Ontology` methods, each doing one thing:
- `_open_contradiction_if_flagged_member(assertion_id) -> Contradiction | None` — the shared read (open contradiction the assertion currently belongs to as a flagged member, or `None`).
- `_meets_retract_contradiction_floor(principal, delegating) -> bool` — the shared capability/kind check (delegation-attenuated via `min_capability`, mirroring `_check_direct_write_capability`'s existing pattern; AI-kind checked on the acting principal only).
- `_retract_op_review_override(proposal, principal, delegating) -> RequireReview | None` — combines the two: if `proposal` stages a `retract` op targeting a flagged contradiction member the principal can't clear the floor for, returns a `RequireReview` decision to use instead of evaluating policy; otherwise `None`.

Both `retract()` and `resubmit()` call `_retract_op_review_override` — but **only within the branch where `self.policy`/re-evaluation would otherwise auto-accept**, and only *after* the pre-existing party-to-contradiction guard (`_reject_retract_if_party_to_contradiction`, KI-033) has already run and passed for that same branch. A proposal `self.policy` was already going to send to review for unrelated reasons (e.g. the author's own base capability) skips both checks entirely at submission/resubmission time — exactly as the party guard has always behaved — and both are deferred to `accept_proposal()`, whose accepting reviewer already satisfies (or is itself checked against) the relevant floor.

`_require_capability_to_retract_flagged_member(assertion_id, principal, delegating)` — the original, raising form of the capability check — still exists, but now only as an **authoritative, in-transaction backstop** for the narrow race where a contradiction opens concurrently between the optimistic pre-check above and the write transaction. Called from `retract()`'s own commit path and, via a `retracting_principal: tuple[Principal, Principal | None]` parameter threaded through from `resubmit()`'s already-resolved principal/delegate (not re-derived — see Consequences), `_replay_proposal_operations`'s `retract` branch when reached from `resubmit`'s auto-accept (never from `accept_proposal`, whose reviewer already covers this).

## Rationale

**Why route to review instead of just raising `CapabilityError`:** the first version of this fix did exactly that — raise unconditionally — and review found a genuine capability inversion: a `write`-capability principal's retraction was flatly rejected with no path forward (`ThresholdPolicy` auto-accepts `write` unconditionally, so there was no way for that principal to get a proposal into the review queue at all), while a merely `propose`-capability principal's retraction sailed through the ordinary review queue untouched, since it never reached the new check in the first place. A *higher*-capability principal ended up strictly worse off than a lower one for the identical action. Routing to review instead removes the inversion: every principal gets the same two outcomes regardless of capability — auto-accept if they meet the floor, queue for review if they don't — matching how every other capability-shortfall case in this codebase already works (AI authorship, low-`propose`-capability authors generally).

**Why the party guard must run before the capability-floor override, and only within the would-auto-accept branch:** these two constraints came from the same discovery during review. Initially the capability-floor override ran unconditionally, before the party guard — which broke `test_party_cannot_retract_opposing_contradiction_member` and friends: a `write`-capability *party* got routed to review instead of being immediately rejected, when the correct behavior (per KI-033, pre-dating this ADR) is an immediate, unconditional block regardless of capability — queuing it for review would only defer an already-certain rejection (the party guard also runs, unconditionally, inside `accept_proposal`'s replay) to whoever eventually reviews it, turning a clear error into a silently-doomed pending proposal. Conversely, checking *both* guards unconditionally (even when policy was already going to require review for unrelated reasons) broke `test_party_via_accept_proposal_is_also_blocked`, which specifically pins that a low-capability delegate's retract proposal reaches the review queue *without* the party guard firing early — the guard is meant to fire only when it's gating an actual auto-accept bypass, deferring to `accept_proposal` otherwise. The fix that satisfies both: evaluate `self.policy` first; only if it says `AutoAccept` do the party check (immediate, unconditional block if it fires) and then the capability-floor override (route-to-review if it fires) — in that order, and only in that branch.

**Why a sibling method instead of merging into `_reject_retract_if_party_to_contradiction`:** the two guards answer different questions with different failure semantics and different actor sets, and now different *outcomes* too (unconditional block vs. route-to-review) — collapsing them into one method would have made the ordering/branching logic above much harder to follow than two small, single-purpose methods composed explicitly at each call site.

**Why `accept_proposal` needs no new check:** `_require_reviewer_principal` (existing, unconditional precondition of `accept_proposal`) already requires `review`/`admin` capability and blocks AI-kind reviewers — precisely the floor this ADR wants enforced. Re-checking would be pure redundancy with no behavioral difference.

**Why delegation attenuates via `min`, not the delegating principal's capability alone:** mirrors `_check_direct_write_capability`'s existing, established precedent (SPEC §8.4 — effective capability is `min(principal, delegating)`, never a wholesale substitution). A `write`-only principal can't reach the elevated floor by delegating to/from a `review`-capable one in either direction; both ends of the delegation must independently clear the bar.

**Why AI-kind is checked on the acting principal only, not the delegating one:** matches `_check_direct_write_capability`'s own precedent (and ADR-0003's framing generally) — an AI's accountable owner is the eligible actor once delegation is involved, not the AI itself.

**Why `resubmit()` threads `(principal, delegating)` into `_replay_proposal_operations` instead of letting it re-derive them:** an earlier version of this fix re-fetched the delegating principal fresh (`backend.get_principal(proposal.acting_as)`), which fails *open* on a missing/invalid `acting_as` (silently treated as "no delegation" — no attenuation) instead of the fail-*loud* `AuthError`/`CapabilityError` `_resolve_delegation` already raises when validating delegation at submission/resubmission time. Since `resubmit()` already resolves `principal`/`delegating` correctly via `_resolve_delegation` before ever reaching the replay, passing that same pair through removes both the duplicated lookup and the fail-open gap in one change.

## Consequences

**Positive:**
- Closes KI-043: retracting a disputed static fact now requires the same review-grade floor `resolve_contradiction()` already enforces, everywhere, without leaving any principal capability-wise worse off than a lower-capability one for the same action.
- No new capability-checking machinery — reuses the same `min_capability`/delegation-attenuation pattern already established by `_check_direct_write_capability` and the same AI-kind precedent.

**Negative / follow-ups:**
- **Behavior change**: a deployment with automation or scripts that previously retracted flagged contradiction members using only `write`-capability service/human principals will now see that call return a `require_review` proposal instead of an immediately-retracted assertion — the retraction no longer takes effect until a `review`-capable, non-AI principal calls `accept_proposal()`. Ordinary (non-contradiction) retraction is unaffected. Flagged as `**Breaking:**` in CHANGELOG per ADR-0019's policy, even though no public *signature* changed — this is the class of behavior-level break ADR-0019 asks to be called out regardless.
- **No audit trace for the narrow race-only `CapabilityError`.** If a contradiction opens concurrently between the optimistic pre-check and the write transaction, `_require_capability_to_retract_flagged_member` still raises and rolls back — nothing is persisted, matching this project's existing precedent for pre-transaction-check failures elsewhere (e.g. KI-042's validator rejections). This path is exercised only by unit tests calling the backstop method directly (`tests/unit/test_retract_contradiction_capability_floor.py`), not end-to-end — reliably constructing the actual race without mocking internals wasn't judged worth the complexity for a window this narrow.
- `_retract_op_review_override`/`_require_capability_to_retract_flagged_member` both re-fetch the target assertion and open contradiction independently (once optimistically, once authoritatively) — two extra backend round-trips on every retract of a flagged member, beyond what `_reject_retract_if_party_to_contradiction`'s own pre-existing read already costs. Not optimized; retraction is not a hot path.

## Alternatives Considered

**Raise `CapabilityError` unconditionally instead of routing to review:** Rejected after review — see Rationale's first point. This was the original design and it created a capability inversion with no remediation path for the principal it blocked.

**Force every retract of a flagged member through the proposal/review queue, rather than gating auto-accept specifically:** Rejected — `retract()` already goes through the proposal/policy path (`ThresholdPolicy` decides accept vs. review based on the *caller's* capability); a `review`-capable principal calling `retract()` directly already auto-accepts today and should continue to, matching `resolve_contradiction()`'s own directness. Forcing review unconditionally would make a `review`-capable principal's retraction slower than an equally-privileged `resolve_contradiction()` call for no added safety.

**Check capability inside `ThresholdPolicy` itself (a policy-level rule) instead of a domain-layer check in `ontology.py`:** Rejected — `PolicyStrategy.evaluate()` doesn't have visibility into *which* assertion a retract targets in a way that's natural to inspect for contradiction-membership, and `govern/policy.py` is explicitly pure/deterministic (no I/O) per this project's core invariants — reaching into contradiction state from there is a layering violation `_reject_retract_if_party_to_contradiction`'s own precedent already avoided.

## References

- SPEC §10.3 (Contradiction resolution — review-routed, not auto-resolved)
- ADR-0003 (AI proposals always require review — delegation-and-AI-kind precedent)
- `src/ontolith/ontology.py` (`_retract_op_review_override`, `_meets_retract_contradiction_floor`, `_open_contradiction_if_flagged_member`, `_require_capability_to_retract_flagged_member`, `_reject_retract_if_party_to_contradiction`, `_check_direct_write_capability`, `resolve_contradiction`)
- `conformance/test_contradiction_resolution.py` (`TestRetractContradictionGuard`)
- `tests/unit/test_retract_contradiction_capability_floor.py` (resubmit-path routing, and direct coverage of the race-only backstop)
- `docs/known-issues.md` (KI-026, KI-033, KI-034, KI-043)
