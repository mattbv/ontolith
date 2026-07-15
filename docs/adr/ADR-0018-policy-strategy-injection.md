# ADR-0018: PolicyStrategy Injection, Deferring the SPEC's `kb` Parameter

**Status**: Accepted
**Date**: 2026-07-14
**Deciders**: Ontolith Core Team
**Related**: ADR-0006 (Licensing & Business — PolicyStrategy as an open-core plugin seam), SPEC §9.2 (Policy engine), SPEC §14 (Plugin protocols)

---

## Context

`ThresholdPolicy` is the only `PolicyStrategy` implementation, and it is hardcoded: `Ontology._apply_with_conflict_routing`, `propose`, and `propose_ref` (three call sites in `src/ontolith/ontology.py`) each construct `ThresholdPolicy()` inline. Neither `Ontology.__init__` nor `Ontology.connect` accepts a `policy` argument. ADR-0006 states that `PolicyStrategy` is an open-core extension point — proprietary policy implementations (`ConfidenceThreshold`, `SourceQuorum`, `RequireReviewByRole`, etc., per SPEC §9.2's SHOULD list) are meant to "slot in naturally." Today they cannot slot in at all; there is no injection point.

Separately, SPEC §9.2 and §14 both specify `PolicyStrategy.evaluate(self, proposal, principal, kb: ReadOnlyView) -> Decision`, and SPEC §9.2 states `evaluate` **MUST** be pure ("no writes, deterministic given inputs") "so it is testable and replayable." The actual `PolicyStrategy` Protocol (`src/ontolith/govern/policy.py`) has `evaluate(self, proposal, principal, acting_as=None)` — no `kb` parameter, and an `acting_as` parameter the SPEC signature doesn't name (needed for SPEC §8.4 delegation, which `ThresholdPolicy` already implements).

Adding `kb: ReadOnlyView` is not blocked by the SPEC's own purity bar — "no writes, deterministic given inputs" permits reads, provided the KB state read is treated as part of the evaluation's inputs. What it *does* require, which isn't yet designed, is a defined answer to "deterministic given inputs as of when": a live `ReadOnlyView` over an open, potentially-concurrently-mutated connection has no fixed state to be deterministic *against* unless evaluation is pinned to a specific snapshot (e.g., an `as_of`-style view at proposal-creation time). SPEC §9.2's own stated goal — "testable and replayable" — needs exactly that: a decision must be reproducible later, which means the KB state it was evaluated against must be capturable, not just live-readable.

## Decision

Split this into two decisions, resolved differently:

**1. Make `PolicyStrategy` actually injectable now.** Add `policy: PolicyStrategy | None = None` to both `Ontology.__init__` and `Ontology.connect`, defaulting to `ThresholdPolicy()` when omitted, stored as `self.policy`. Replace the three inline `ThresholdPolicy()` constructions with `self.policy`. This closes the "not actually pluggable despite being declared a plugin seam" gap with no signature change to `PolicyStrategy.evaluate()` itself.

**2. Defer the `kb: ReadOnlyView` parameter.** `PolicyStrategy.evaluate()` keeps its current pure signature (`proposal`, `principal`, `acting_as`). The SPEC's `kb` parameter is a known, deliberate deviation — not implemented in this pass — because giving policies a KB view that satisfies SPEC §9.2's own "testable and replayable" bar requires a snapshot/consistency contract that doesn't exist yet, and designing that speculatively — without a concrete KB-inspecting strategy (e.g. `SourceQuorum`) to validate it against — risks picking the wrong shape.

## Rationale

**Why unblock injection without resolving the `kb` question first:**
- The two problems are independent. "Can a caller supply a custom policy at all" and "can that policy read the KB" are separable — fixing the first doesn't foreclose the second, and every non-KB-inspecting policy (`ConfidenceThreshold`, `RequireReviewByRole`, `TrustLevel` — three of SPEC §9.2's four SHOULD-have strategies) is fully implementable today with the pure signature.
- Shipping the injection point now, with the pure signature, means any of those three can be built and used immediately; only `SourceQuorum` is blocked, and it was already unbuildable before this ADR too.

**Why not just add `kb` now:**
- SPEC §9.2 doesn't just require "no writes" — it requires the evaluation to be reproducible later ("testable and replayable"). A live `ReadOnlyView` handed to `evaluate()` has no answer to "reproducible against what state" — the KB keeps changing after the decision is made. Getting this right likely means evaluating against an `as_of`-pinned view (state as of proposal creation, or as of evaluation time — both defensible, SPEC doesn't say which), which is real design work, not a mechanical parameter addition.
- No concrete `SourceQuorum`-style strategy exists yet to design the snapshot contract against. Building the plumbing speculatively, ahead of a real consumer, risks committing to a shape (e.g. "as of proposal creation") that turns out wrong once an actual KB-inspecting policy needs something else (e.g. "as of evaluation time, ignoring assertions still under review").

**Why this is a real, not hypothetical, gap worth an ADR (not silent deferral):**
- ADR-0006 already promises this extension point publicly as part of the open-core boundary. Silently leaving `PolicyStrategy` non-injectable would contradict a standing architectural claim; recording the gap and the reasoning keeps the paper trail honest for whoever builds the next proprietary policy strategy.

## Consequences

**Positive:**
- `PolicyStrategy` is now genuinely pluggable — `Ontology(backend, policy=MyPolicy())` and `Ontology.connect(path, policy=MyPolicy())` both work.
- Three of SPEC §9.2's four suggested strategies are fully buildable today.
- No behavior change for existing callers (default remains `ThresholdPolicy()`).

**Negative / follow-ups:**
- `SourceQuorum`-style KB-inspecting policies remain unbuildable until the `kb` parameter's snapshot/replay contract is designed. Tracked in `docs/known-issues.md`.
- `PolicyStrategy.evaluate()`'s signature still diverges from the SPEC's literal text (`acting_as` present, `kb` absent). This ADR is the record of why; SPEC §9.2/§14 should eventually be updated to match, or superseded by a future ADR once the `kb` question is resolved either way.

## Alternatives Considered

**Add `kb: ReadOnlyView` now, evaluated live against current KB state:** Rejected for this pass — satisfies SPEC §9.2's "no writes" clause but not its "testable and replayable" clause, since a live view's state isn't pinned to anything reproducible. See Rationale.

**Pass a frozen/snapshotted read-only data structure instead of a live `ReadOnlyView`:** The likely eventual answer, but requires knowing in advance *what* data a policy might need without knowing what policies will exist — premature design for a use case (`SourceQuorum`) that isn't being built yet. Worth building when it is.

**Don't add injection until the `kb` question is resolved (block both on one decision):** Rejected — unnecessarily couples two independent gaps and leaves three fully-specifiable, non-KB-inspecting strategies (SPEC §9.2) blocked on a fourth's harder design question.

## References

- ADR-0006 (Licensing & Business — plugin extension points)
- SPEC §9.2 (Policy engine — purity requirement, `evaluate` signature), §14 (Plugin protocols — `PolicyStrategy`)
- `src/ontolith/govern/policy.py` (`PolicyStrategy`, `ThresholdPolicy`)
