# ADR-0045: `ConfidenceThreshold`, `SourceRequired`, `RequireReviewByRole` — the Last Three SPEC §9.2 Strategies

**Status**: Accepted

**Date**: 2026-09-04

**Deciders**: Ontolith Core Team

**Related**: SPEC §9.2 (policy engine contract — names six built-in strategies a conforming
implementation SHOULD provide), ADR-0025 (`SourceQuorum` — the KB-inspecting-strategy precedent
these three follow for the KI-015 capability floor), ADR-0040 (`Composite` — the combinator these
three are designed to compose with, not duplicate), ADR-0003 (AI accountable-owner/always-review
rule — the "own kind never laundered via delegation" precedent `RequireReviewByRole`'s role lookup
follows), `docs/known-issues.md` KI-069 (closed by this ADR), KI-015 (the capability-floor rule
every non-`ThresholdPolicy` strategy must reimplement)

---

## Context

SPEC §9.2 names six built-in `PolicyStrategy` implementations a conforming implementation SHOULD
provide: `ConfidenceThreshold`, `TrustLevel`, `SourceRequired`, `RequireReviewByRole`,
`SourceQuorum`, and `Composite(all=…, any=…)`. By M3's close, three existed: `ThresholdPolicy`
(the shipped default, covering roughly what `TrustLevel` would — capability and trust-level
gating), `SourceQuorum` (ADR-0025), and `Composite` (KI-061, ADR-0040). `ConfidenceThreshold`,
`SourceRequired`, and `RequireReviewByRole` remained entirely unbuilt — no strategy in the
codebase covered confidence-based routing, requiring a non-empty `source`, or role-based reviewer
assignment. KI-069 (filed while closing KI-061, when ADR-0040's own Consequences section named
this as the last unaddressed gap) asked for these three to be implemented, or explicitly and
individually decided out of scope.

SPEC §9.2 names each strategy but does not define its exact semantics beyond the name — the same
situation `SourceQuorum` and `Composite` were each in before their own ADRs filled the gap with a
reasoned design. This ADR does the same for the remaining three.

## Decision

**All three are implemented**, none deferred. Each is a small, independent, leaf `PolicyStrategy`
in `govern/policy.py`, following the exact structural conventions `SourceQuorum` already
established: a plain class (not a dataclass, matching every existing strategy), a constructor that
validates its own configuration and raises `ValueError` on nonsense input, an `evaluate()` that
inspects only the proposal's *first* staged operation (every current caller of
`propose`/`propose_ref`/`retract` stages exactly one), and explicit enforcement of the KI-015
read-capability floor (`propose`/`propose_ref`/`retract` have no capability pre-check of their
own — every strategy besides `ThresholdPolicy` must reimplement this floor itself, or a
read-capability principal's proposal could otherwise slip through as `AutoAccept`).

### `ConfidenceThreshold(threshold, reviewers=None)`

Auto-accepts once the proposal's own staged `confidence` (`op["confidence"]`, the value
`propose`/`propose_ref`'s own `confidence` parameter already produces) meets `threshold`
(inclusive comparison, `>=`). `threshold` must be within `[0.0, 1.0]` — SPEC's own confidence
scalar range (`Assertion.confidence: float | None = Field(None, ge=0.0, le=1.0)`) — or the
constructor raises `ValueError`.

**Missing confidence (`None`) always requires review, never auto-accepts, regardless of
`threshold`.** This is the one genuinely non-obvious call in this strategy: treating "no stated
confidence" as confidence `0` (always fails a positive threshold) or confidence `1` (always
passes) would both be guesses this strategy has no basis to make. SPEC's core invariant that
confidence is "NEVER auto-combined in v1" is a warning against inventing values where none were
asserted — this strategy takes that literally rather than picking an implicit default.

Retractions (`Ontology.retract`'s payload has no `confidence` key at all) and unrecognized op
kinds always require review, matching `SourceQuorum`'s identical treatment of the same two cases.

### `SourceRequired(reviewers=None)`

Auto-accepts only when the operation's `source` is present and non-empty; requires review
otherwise. No `kb` read, no corroboration counting — a narrower, unconditional cousin of
`SourceQuorum`, which counts *distinct* sources across the proposal and matching existing
assertions. A deployment wanting both ("every fact needs a source, AND at least 2 of them")
composes `Composite(all=[SourceRequired(), SourceQuorum(2)])` rather than either strategy
reimplementing the other's job. Retractions and unrecognized kinds require review, same as the
other two strategies.

### `RequireReviewByRole(role_reviewers, *, default=None)`

The odd one out: **it never auto-accepts, and never inspects the proposal's payload at all.**
Every proposal it evaluates lands in `require_review` — its entire purpose is *which reviewers*,
not *whether* review is needed. Used alone, this makes every proposal require review; a
deployment that also wants some proposals to skip review composes it with an accepting strategy
via `Composite(any=[...])`.

**"Role" has no dedicated field on `Principal`.** SPEC's `Principal` model (§8.1) has `kind`
(human/ai/service), `default_capability`, `trust_level`, `owner`, and an open `metadata: dict[str,
Any]` blob explicitly documented as "for future extension." This strategy reads
`principal.metadata.get("role")` — using the extension point the codebase already ships for
exactly this kind of not-yet-formalized, deployment-specific attribute, rather than adding a new
typed field to `Principal` for a single strategy's use. See Alternatives Considered for what was
rejected and why.

Role is looked up on `principal` (the real author), never on `acting_as`. This mirrors
`ThresholdPolicy`'s own "AI principals always require review, regardless of delegation" rule
(ADR-0003): the author's real identity governs, and delegation cannot be used to borrow a
different role's (and therefore a different, possibly more lenient, reviewer set's) routing. The
KI-015 capability floor still uses the effective (`acting_as`-aware) capability, exactly like
`ThresholdPolicy`/`SourceQuorum` — capability delegation and role lookup are deliberately
different rules with different laundering resistance, matching how `ThresholdPolicy` itself
already treats `kind` (never delegated) and capability (delegated, capped at the minimum)
differently.

A missing role and an unmapped role both fall back to `default` — never an error, never a bare
empty `RequireReview` with no explanation. The two cases return distinguishable reason text ("no
declared role" vs. "no reviewers configured for role X") so a caller inspecting `decision.reason`
can tell them apart without re-deriving the principal's metadata itself.

## Rationale

**Why these three didn't need a `kb` read (unlike `SourceQuorum`):** each decides purely from data
already sitting on the proposal or the principal — `op["confidence"]`, `op["source"]`,
`principal.metadata["role"]`. None of them need to know what else is in the knowledge base. This
mirrors `ThresholdPolicy`'s own shape (also `kb`-independent, also defaults `kb` to `None` for
caller convenience) more than `SourceQuorum`'s (which genuinely reads `kb.assertions(...)` to count
corroboration).

**Why capability floor enforcement is duplicated three more times instead of factored into a
shared base class or helper:** `SourceQuorum` already has this exact
`capability`/`min_capability`/read-rejection block (`policy.py`'s `SourceQuorum.evaluate`, its
first few statements), and this ADR follows that established convention rather than introducing a
new one unilaterally mid-KI (`ThresholdPolicy`'s own version
is a related but not identical shape — it also derives an effective `trust_level` and checks the
read-rejection after its write/admin auto-accept branches, since it has more capability tiers to
route than a binary accept/review split needs). A future refactor extracting a shared
`_effective_capability(principal, acting_as)` helper is straightforward and backward compatible
whenever someone wants it — not blocking for this ADR's own scope.

**Why `RequireReviewByRole` doesn't validate that `role_reviewers`' keys are non-empty, unlike
`Composite`'s "at least one strategy" constructor guard:** `Composite()` with nothing to compose
can never produce a valid decision (`evaluate()` would have nothing to combine) — that's a genuine
construction-time error. `RequireReviewByRole({})` is well-defined: every proposal routes to
`default` (possibly also empty), the same as `ThresholdPolicy`'s own `reviewers=[]` default case.
An empty configuration isn't a mistake here the way an empty `Composite` is.

## Alternatives Considered

**A dedicated `Principal.role: str | None` field instead of reading `metadata["role"]`:** rejected
— would require a `Principal` schema/DB-migration change (a new column, SPEC §8.1 model update)
motivated by a single, optional `PolicyStrategy`'s needs, when the existing `metadata` blob is
purpose-built for exactly this. If "role" turns out to be a cross-cutting concept multiple
strategies or interfaces need, promoting it to a first-class field later is a compatible,
additive change — nothing about this ADR forecloses that.

**Keying `RequireReviewByRole` by `principal.kind` (human/ai/service) instead of an open role
string:** rejected — `kind` is already the dimension `ThresholdPolicy`'s own AI-always-review rule
uses; a strategy that only re-slices `kind` adds no new expressiveness SPEC's naming implies
("role" reads as an organizational/functional concept — legal, engineering, compliance — distinct
from the human/ai/service taxonomy `kind` already covers).

**Making `ConfidenceThreshold` treat missing confidence as `0.0` (fails every positive
threshold) or reject it outright instead of requiring review:** rejected for the former (an
implicit default the strategy has no basis to assume, per the Decision section above); rejected
for the latter because `Reject` is terminal (SPEC §9.1) and a missing confidence value is a data
gap, not grounds to permanently refuse the proposal — `RequireReview` lets a human resolve it
either way.

**`SourceRequired` composed permanently into `SourceQuorum` (i.e., making `SourceQuorum` itself
reject sourceless proposals more strongly) instead of a separate strategy:** rejected —
`SourceQuorum` already rejects sourceless proposals (`RequireReview`, "cannot be established"); a
deployment that wants source-presence enforced *without* also wanting quorum counting (e.g.
threshold=1 effectively degenerates toward this, but pays quorum's `kb` read for no reason) is
better served by a dedicated, `kb`-free strategy. `Composite` still lets both be combined when a
deployment genuinely wants both rules.

## Consequences

**Positive:**
- Closes KI-069: all six SPEC §9.2 SHOULD-list strategies now exist (`ThresholdPolicy` for
  `TrustLevel`, plus `SourceQuorum`, `Composite`, and these three).
- Each new strategy follows the exact structural and documentation conventions `SourceQuorum`
  established, keeping `govern/policy.py` uniform rather than introducing a divergent pattern for
  the "last three."
- `RequireReviewByRole`'s use of `Principal.metadata` needs no schema change, no migration, and no
  new port method — usable immediately with any existing `Principal`.

**Negative / follow-ups:**
- `principal.metadata["role"]` is an unvalidated, untyped string set by whoever creates the
  principal (`create_principal`'s `metadata` parameter has no schema) — a typo in a configured
  role name (`"Legal"` vs `"legal"`) silently falls through to `default` rather than erroring.
  This is the same trade-off `AdminEvent.actor` already accepts for a different unvalidated field
  (ADR-0042) — a deliberate, documented gap, not an oversight, but a real one worth a deployment's
  own awareness when configuring roles.
- None of these three strategies special-case AI-authored proposals (matching `SourceQuorum`'s own
  documented choice, ADR-0025 §5) — a deployment wanting `ConfidenceThreshold`/`SourceRequired`
  combined with "AI always requires review" needs `Composite`, the same as for `SourceQuorum`
  today.
- `RequireReviewByRole`'s role-reviewer mapping is fixed at construction (a plain `dict`, not
  itself reloadable at runtime) — a deployment that wants to change role→reviewer assignment
  without restarting needs to construct a new policy instance and swap it in (`Ontology.policy` is
  already a public, reassignable attribute, used exactly this way in this codebase's own test
  suite).
- **`ConfidenceThreshold`/`SourceRequired` gate on author-controlled data, not corroborated KB
  state — a materially weaker guarantee than `SourceQuorum`'s own precedent, worth stating
  plainly.** Both read a value the proposal's own author supplied (`confidence`/`source` are
  `propose()`/`propose_ref()` parameters, self-attested at call time); an AI principal holding only
  `propose` capability (KI-015 has no author-side capability pre-check) can auto-accept its own
  write simply by asserting `confidence=1.0` or `source="anything"`. `SourceQuorum` at least
  requires *persisted, independently-authored* corroborating assertions before auto-accepting — a
  deployment relying on either new strategy alone should understand it is trusting the author's own
  stated confidence/sourcing, not verifying it against anything.
- **`RequireReviewByRole`'s chosen `reviewers` are not currently consumed anywhere in the
  system** (found in review): `Proposal` has no `reviewers` field, `Ontology`'s
  non-auto-accept path persists only `policy_reason`, and no `assign` review action exists yet
  despite SPEC §9.4 naming one. Every other strategy's `RequireReview.reviewers` has this same
  gap — `RequireReviewByRole` just makes it far more consequential, since routing reviewers by
  role is its entire stated purpose (see Decision above), not an incidental detail the way it is
  for `ThresholdPolicy`'s empty-list default. Today, only a direct SDK caller inspecting the
  returned `Decision` object sees the assignment; REST/MCP/CLI callers see only the reason string
  (which does name the role, e.g. `"Requires review by role 'legal' (principal: a@x.com)"`).
  Wiring `reviewers` through to a persisted, queryable review-assignment surface is real,
  pre-existing scope (SPEC §9.4's `assign` action) that this ADR does not attempt — tracked
  separately as KI-078, filed alongside this ADR.
- `ConfidenceThreshold`'s own `threshold` is validated to `[0.0, 1.0]` at construction, but the
  *proposal's* confidence value it compares against is not independently range-checked at the
  policy layer — `propose()`/`propose_ref()` type `confidence` as `float | None` with no range
  enforcement of their own (SPEC-level `[0.0, 1.0]` enforcement only happens later, on
  `Assertion`'s own field validator, and only on the auto-accept path). A confidence outside
  `[0.0, 1.0]` (e.g. `1.5`) is compared as given — a pre-existing input-validation gap upstream of
  this strategy, not introduced by it, but worth knowing this strategy doesn't add a defensive
  check of its own.

## References

- SPEC §9.2: Policy engine contract
- SPEC §8.1: Principal model (`metadata: dict[str, Any]`, documented as open for future extension)
- ADR-0025: `PolicyStrategy`'s `kb` parameter and `SourceQuorum`
- ADR-0040: `Composite` policy strategy
- ADR-0003: Agent identity (AI accountable-owner, always-review rule — the delegation-laundering
  precedent `RequireReviewByRole`'s role lookup follows)
- ADR-0042: Admin-Action Audit Trail (the closest existing precedent for an unvalidated,
  caller-supplied attribution string — `AdminEvent.actor` — the same trade-off this ADR accepts
  for `principal.metadata["role"]`)
- `docs/known-issues.md` KI-069 (resolved by this ADR), KI-015 (the capability-floor rule these
  three strategies each reimplement)
