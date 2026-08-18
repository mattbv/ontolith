# ADR-0030: `retract()` Requires `resolve_contradiction()`'s Capability Floor for Flagged Contradiction Members

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

New `Ontology._require_capability_to_retract_flagged_member(assertion_id, principal, delegating)`, a sibling to the existing `_reject_retract_if_party_to_contradiction` (KI-033), no-ops the same way when the target isn't a flagged member of an open contradiction, and otherwise raises `CapabilityError` if the principal is AI-kind or its effective capability (after delegation attenuation, SPEC §8.4 — `min(principal, delegating)` when delegating, mirroring `_check_direct_write_capability`'s existing pattern) is below `review`.

Called from the two places a retraction can land without any independent reviewer already having been capability-gated:
- `retract()`'s own auto-accept branch (a `write`-capability principal's retraction commits immediately, no human review involved).
- `_replay_proposal_operations`'s `retract` branch, but **only** when called from `resubmit`'s own auto-accept branch (`extra_retracting_party is None`) — not from `accept_proposal`, whose accepting reviewer already satisfies this exact floor via the pre-existing `_require_reviewer_principal` check, making a second check there redundant.

## Rationale

**Why raise the floor instead of leaving it as documented, intentional behavior:** the KI's own framing — surfaced as an inconsistency during review, not as a deliberate design choice defended anywhere — is the core reason. Nothing in SPEC §10.3 or any prior ADR argues retract-based contradiction disturbance should be *easier* to reach than resolution-based; the asymmetry was an oversight (retract() predates the contradiction-guard work KI-033/KI-034 added incrementally, each closing one dimension of the gap without revisiting the capability floor itself), not a considered tradeoff.

**Why the check is a sibling method, not merged into `_reject_retract_if_party_to_contradiction`:** the two guards answer different questions with different failure semantics and different actor sets. The party guard's `parties` set deliberately includes the accepting reviewer for `accept_proposal` (a reviewer who is themselves a party shouldn't get to approve either side) — but that same broadening is wrong for the capability floor, which must apply to the *original author's* capability only when there's no reviewer to defer to (`retract()`'s own auto-accept, `resubmit`'s auto-accept), and must NOT apply to the original author when there IS a reviewer (`accept_proposal`) — a low-`propose`-capability author's retract proposal reaching review and being accepted by a genuinely `review`-capable principal is exactly the intended, unblocked path. Reusing one `parties`-shaped check for both would have required threading two different semantics through one set, which is more error-prone than two small, single-purpose methods.

**Why `accept_proposal` needs no new check:** `_require_reviewer_principal` (existing, unconditional precondition of `accept_proposal`) already requires `review`/`admin` capability and blocks AI-kind reviewers — precisely the floor this ADR wants enforced. Re-checking would be pure redundancy with no behavioral difference, so it's deliberately skipped (see `_replay_proposal_operations`'s docstring, which now states this explicitly) rather than added "for consistency" at zero benefit.

**Why delegation attenuates via `min`, not the delegating principal's capability alone:** mirrors `_check_direct_write_capability`'s existing, established precedent (SPEC §8.4 — effective capability is `min(principal, delegating)`, never a wholesale substitution). A `write`-only principal can't reach the elevated floor by delegating to/from a `review`-capable one in either direction; both ends of the delegation must independently clear the bar.

**Why AI-kind is checked on the acting principal only, not the delegating one:** matches `_check_direct_write_capability`'s own precedent (and ADR-0003's framing generally) — an AI's accountable owner is the eligible actor once delegation is involved, not the AI itself; the AI-kind check exists to stop an AI from being the one making the call, not to forbid a human from ever delegating through/to an AI-owned relationship.

## Consequences

**Positive:**
- Closes KI-043: retracting a disputed static fact now requires the same review-grade floor `resolve_contradiction()` already enforces, everywhere.
- No new capability-checking machinery — reuses the same `min_capability`/delegation-attenuation pattern already established by `_check_direct_write_capability` and the same AI-kind precedent.

**Negative / follow-ups:**
- **Behavior change**: a deployment with automation or scripts that previously retracted flagged contradiction members using only `write`-capability service/human principals will start seeing `CapabilityError` for that specific case. Ordinary (non-contradiction) retraction is unaffected. Flagged as `**Breaking:**` in CHANGELOG per ADR-0019's policy, even though no public *signature* changed — this is the class of behavior-level break ADR-0019 asks to be called out regardless.
- This check runs *after* `_reject_retract_if_party_to_contradiction` at both call sites — a retracting principal who is both a party AND under-capability sees the "party to" error, not the capability one. Order wasn't treated as a design decision worth optimizing (both ultimately raise `CapabilityError` and block the write); a caller inspecting the exact message would see whichever guard happens to run first.
- `resubmit`'s auto-accept branch re-derives the author/delegate `Principal` objects via a fresh `backend.get_principal()` call rather than reusing anything cached from submission time — consistent with `_replay_proposal_operations`'s existing "trust the original submission's shape, but re-resolve what actually needs re-resolving" precedent (temporality is the other example), not a new pattern.

## Alternatives Considered

**Force every retract of a flagged member through the proposal/review queue, rather than gating auto-accept specifically:** Rejected — `retract()` already goes through the proposal/policy path (`ThresholdPolicy` decides accept vs. review based on the *caller's* capability); a `review`-capable principal calling `retract()` directly already auto-accepts today and should continue to, matching `resolve_contradiction()`'s own directness. Forcing review unconditionally would make a `review`-capable principal's retraction slower than an equally-privileged `resolve_contradiction()` call for no added safety.

**Check capability inside `ThresholdPolicy` itself (a policy-level rule) instead of a domain-layer check in `ontology.py`:** Rejected — `PolicyStrategy.evaluate()` doesn't have visibility into *which* assertion a retract targets in a way that's natural to inspect for contradiction-membership (would require the policy to query `kb` for open contradictions and cross-reference `assertion_id`, logic that belongs with the rest of the contradiction-guard machinery already living in `ontology.py`, not duplicated into every `PolicyStrategy` implementation). Also, `govern/policy.py` is explicitly pure/deterministic (no I/O) per this project's core invariants — reaching into contradiction state from there is a layering violation `_reject_retract_if_party_to_contradiction`'s own precedent already avoided.

## References

- SPEC §10.3 (Contradiction resolution — review-routed, not auto-resolved)
- ADR-0003 (AI proposals always require review — delegation-and-AI-kind precedent)
- `src/ontolith/ontology.py` (`_require_capability_to_retract_flagged_member`, `_reject_retract_if_party_to_contradiction`, `_check_direct_write_capability`, `resolve_contradiction`)
- `conformance/test_contradiction_resolution.py` (`TestRetractContradictionGuard`)
- `docs/known-issues.md` (KI-026, KI-033, KI-034, KI-043)
