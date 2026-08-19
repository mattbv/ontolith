# ADR-0031: Retraction Is Terminal for `resolve_contradiction()` Winner Selection Too

**Status**: Accepted
**Date**: 2026-08-19
**Deciders**: Ontolith Core Team
**Related**: SPEC §10.3 (contradiction resolution), KI-026 (self-resolution guard), KI-033 (retract party-to-contradiction guard), KI-034 (retracted stays terminal across extension), KI-044

---

## Context

`resolve_contradiction(contradiction_id, winner_assertion_id, resolver)` gates winner eligibility purely on `winner_assertion_id in contradiction.member_ids` (SPEC §10.3) — it does not check the candidate's current `status`. Since KI-034, a `retracted` member's id deliberately stays in `Contradiction.member_ids` (that list doubles as `_reject_retract_if_party_to_contradiction`'s scan set, not audit-only), so a resolver can pick an already-`retracted` member as the winner: its status flips to `active` (the `self.backend.set_assertion_status(winner_assertion_id, "active")` line), the value reappears in default query results, but `valid_to` stays closed at the original retraction time — an `active` assertion with a closed validity window, a combination nothing else in this codebase produces.

This is pre-existing, not introduced by KI-034, but KI-034 is what makes a `retracted` member persist inside an *open* contradiction long enough for a resolver to plausibly reach this path — before that fix, an extension would have already resurrected it back to `flagged`, an equally wrong but different outcome. Found during KI-034's own review (2026-08-05).

The codebase already treats retraction as terminal in two adjacent places: `_reject_retract_if_party_to_contradiction`'s KI-033 guard, and conflict-routing extension's KI-034 fix (a `retracted` loser is never resurrected to `flagged` when a contradiction is later extended with a new value). Winner selection was the one remaining path where retraction wasn't final.

## Decision

`resolve_contradiction()` now rejects an already-`retracted` `winner_assertion_id` with `ValidationError`, mirroring the existing "winner not a member" check exactly (same exception type, checked in the same membership-validation loop, before any write). A resolver who wants that value active again has no path via `resolve_contradiction()` — they must submit it as a new assertion instead (which will itself flow back through SPEC §10 conflict routing).

## Rationale

**Why reject rather than silently reactivate:** reactivating a `retracted` member produces a state — `active` with a closed `valid_to` — that every other write path in this codebase avoids by construction. `_apply_with_conflict_routing`'s `Supersede`/`Contradict` outcomes and `retract()`/`resubmit()` itself never produce it either. An `active` assertion with a closed validity window is exactly the kind of state a reader (or a downstream bitemporal `as_of()` reconstruction) has no established way to interpret — it isn't "currently valid" in the way `valid_to IS NULL` normally means, and treating it as valid-until-`valid_to` again would silently resurrect a fact-shaped statement whose validity window the codebase already closed.

**Why this is "retraction is terminal," not a new rule:** KI-033 and KI-034 already established the principle for two other paths (self-retraction after being flagged, and contradiction extension). This ADR is the third and (per the current write surface) last place that principle needed enforcing — winner selection was simply the remaining gap, not a fresh design question. Treating it as anything other than "finish applying the rule already adopted" would be inconsistent with no offsetting benefit.

**Why `ValidationError`, not `CapabilityError`:** mirrors the existing "winner not a member" check immediately above it in the same method — both are about the *shape of the request* (naming an ineligible candidate), not about who's asking. The self-resolution guard (party/AI-kind) stays `CapabilityError` since that's about the resolver's own standing, a genuinely different kind of failure.

**Why no path to "un-retract and reactivate" is offered:** a deployment that wants a retracted value active again has an unambiguous, already-existing route — assert it again. That new assertion goes through ordinary SPEC §10 conflict routing (superseding or re-flagging as appropriate) rather than reaching into `resolve_contradiction()`'s narrower "pick among these specific members" contract to special-case a resurrection. Keeping `resolve_contradiction()`'s contract simple (winner must be a currently-eligible member) was preferred over adding a second, narrower reactivation mechanism.

## Consequences

**Positive:**
- Closes KI-044: `resolve_contradiction()` can no longer produce the `active`-with-closed-`valid_to` state.
- No new write-path branching — the fix is a single additional eligibility check next to an existing one, reusing the same member-fetch loop.

**Negative / follow-ups:**
- **Behavior change**: a resolver who was relying on picking an already-`retracted` member to reactivate it (whether intentionally or by not checking status first) now gets `ValidationError` instead. Flagged `**Breaking:**` in CHANGELOG per ADR-0019's policy, despite no public signature change, matching the same class of behavior-level break ADR-0030 (KI-043) was flagged for.
- A resolver hitting this now has to author a fresh assertion for the value instead of reactivating the old one — a minor ergonomic cost, accepted as the correct tradeoff for not reintroducing a semantically ambiguous state.

## Alternatives Considered

**Silently skip an already-retracted candidate and require the resolver to pick a different member:** Rejected — picking a *different* winner than the one requested without telling the caller is a worse failure mode than a clear, immediate `ValidationError`; every other ineligibility check in this method (not a member, resolver is a party) fails loudly rather than silently substituting.

**Allow reactivation but also reopen `valid_to` (set it back to `None`):** Rejected — this would make `resolve_contradiction()` implicitly perform two actions (pick a winner, *and* undo a prior retraction's temporal close) through one call with no explicit signal that the second thing happened; a resurrection is significant enough it should show up as its own explicit write (a fresh assertion), not a side effect of winner selection.

## References

- SPEC §10.3 (Contradiction resolution — winner reactivation)
- KI-033 (retract party-to-contradiction guard — first place retraction was made terminal)
- KI-034 (retracted stays terminal across conflict-routing extension — second place)
- `src/ontolith/ontology.py` (`resolve_contradiction`)
- `conformance/test_contradiction_resolution.py`
- `docs/known-issues.md` (KI-026, KI-033, KI-034, KI-044)
