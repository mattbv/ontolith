# ADR-0050: An Explicit `supersedes` Hint Resolves `time_varying` + `cardinality="many"` Ambiguity

**Status**: Accepted

**Date**: 2026-09-16

**Deciders**: Ontolith Core Team

**Related**: ADR-0017 (cardinality-aware conflict routing for `static` properties, amended by this
ADR for `time_varying`), SPEC §4 (`cardinality`), SPEC §10.1/§10.2 (conflict routing, temporal
supersession), `docs/known-issues.md` KI-080 (closed by this ADR)

---

## Context

`govern/conflict.route()` accepts a `cardinality` parameter, but ADR-0017 only threaded it into
`_route_static` — `_route_time_varying` ignored it entirely and superseded *every* existing
assertion whose validity window overlapped the incoming one and whose value differed, regardless of
what the schema declared. Reproducible directly: declaring `Person.title` as `cardinality="many",
temporality="time_varying"` and asserting two different, genuinely-concurrent values (e.g. two
concurrent job titles) with overlapping windows caused the second write to immediately supersede
the first, collapsing what the schema says should be an independently-tracked pair of concurrent
facts down to one.

ADR-0017 considered and explicitly declined to extend cardinality-aware routing to `time_varying`:

> `time_varying` properties already support multiple coexisting values via non-overlapping windows
> (SPEC §10.2 — e.g. employment history). Cardinality doesn't add anything new there; two
> *overlapping-window*, differing-value `time_varying` assertions are a genuine supersession
> regardless of cardinality (the newer one replaces the older).

That reasoning holds for the single-concurrent-value case (an update to the one fact that's true
right now) but implicitly assumes there is only one logical "slot" to replace — exactly what
`cardinality="many"` says isn't true. `_route_static`'s own solution — "a differing value simply
coexists for `many`" — cannot be ported mechanically, though: `static` properties never supersede
at all (only contradict or coexist), so there is no ambiguity to resolve there. `time_varying`
properties supersede *by design* (SPEC §10.2's "the world changed" model), and window overlap plus
a differing value alone cannot distinguish two genuinely different intents:

- **"This replaces my current value"** — an update to one of several concurrent values (e.g. a
  promotion: "VP Sales" → "SVP Sales"), which should supersede that *one* prior assertion and leave
  every other concurrent value untouched.
- **"This is a new, additional concurrent value"** — a second job title held at the same time,
  which should coexist with every existing one, not replace any of them.

Nothing in the incoming write — value, window, or anything else already on `Assertion` —
distinguishes these two cases. Only the caller knows which one is meant.

## Decision

Extend `assert_literal()`, `assert_ref()`, `propose()`, and `propose_ref()` with an optional
keyword-only `supersedes: str | None = None` parameter naming a specific existing assertion the
incoming one explicitly replaces.

**Routing behavior** (`govern/conflict._route_time_varying`, cardinality="many" only):

- `supersedes=None` (the default): an overlapping differing value **coexists** — `Activate()`,
  mirroring `_route_static`'s own "many" branch, which never auto-supersedes at all. This is the
  behavior change from before this ADR: previously every such value superseded unconditionally.
- `supersedes=<id>`: **exactly** the named assertion is superseded (`Supersede(targets=[id])`).
  Every other overlapping-differing existing assertion is left untouched — a hint for one
  replacement must never silently sweep in unrelated concurrent values.

`cardinality="single"` is completely unaffected — `_route_time_varying`'s "single" branch doesn't
consult `supersedes_hint` at all, since window overlap and a differing value are already
unambiguous there (there is only ever one logical slot).

**Validation** (`Ontology`, ADR-0050's two-layer split):

- **Scope**, checked once at submission time (`_require_valid_supersedes_hint`, mirroring
  `_require_existing_subject`/`_require_existing_target`'s "fail loud before the write reaches
  routing" convention): `supersedes` is rejected outright — `ValidationError` — unless the
  predicate resolves to `temporality="time_varying"` and `cardinality="many"`. Every other
  combination already routes unambiguously without a hint, so a caller supplying one there almost
  certainly misunderstands what it does.
- **Existence, activeness, and conflict membership**, checked by `route()` itself against a fresh
  read of `existing` (`ValueError`, translated to `ValidationError` in
  `_apply_with_conflict_routing`): the hint must name an assertion that is active, on this
  `(subject, predicate)`, whose window overlaps the incoming one, and whose value differs from it —
  exactly SPEC §10.2's own supersession precondition. A hint pointing at a nonexistent assertion, a
  same-value assertion (nothing to replace — that would corroborate, not supersede), or a
  non-overlapping window all raise the same error, since none of them describe a genuine
  replacement.

This two-layer split exists because `propose()`/`propose_ref()` stage their write and only apply it
later, at accept/resubmit time — the scope check runs once, at submission; `route()`'s own
membership check runs again at replay time against whatever is active *then*, the same "checked
once, re-validated fresh at apply time" pattern `temporality` itself already has in
`_replay_proposal_operations`. A schema change between submission and replay that moves the
predicate out of `cardinality="many"`/`temporality="time_varying"` leaves a stored `supersedes` hint
silently unconsulted at replay (the "single"/`static` routing branches never look at it), not
re-validated and rejected late — a narrow, accepted gap matching the existing, documented
temporality-drift precedent for this same method, not a new one.

`Assertion.supersedes` (the persisted field, already existing since before this ADR) is populated
exactly as it always has been — from the `ConflictResult.targets` the router actually returns.
`supersedes` the caller-facing parameter and `Assertion.supersedes` the persisted field share a
name deliberately: the hint, when honored, becomes that field's value.

## Rationale

**Why an explicit hint rather than always coexisting (mirroring `_route_static`'s "many" branch
exactly, with no new parameter):** was seriously considered — the option is not wrong, only more
limited. Always-coexist requires no new API surface and is trivially correct, but it means a
`cardinality="many"` `time_varying` property can never be *corrected* by simply asserting a new
value the way a `single`-cardinality one can — every differing overlapping value would stay
concurrent forever, and fixing a specific one would require an explicit `retract()` first (mirroring
the tradeoff `static`+`many` already lives with, since it never auto-supersedes either). Given the
KI's own motivating example — "VP Sales" corrected to "SVP Sales" alongside a second, genuinely
distinct concurrent title — an always-coexist rule cannot express the correction case at all without
a separate retract-then-reassert round trip, and every `assert_literal`/`assert_ref` caller would
need to learn that two-step dance specifically for `many`-cardinality `time_varying` properties. An
explicit hint keeps the single-call ergonomics every other write path already has, at the cost of a
new parameter four methods must carry and validate.

**Why validate scope eagerly (reject a hint outside `time_varying`+`many`) rather than silently
ignore it everywhere it doesn't apply:** matches this codebase's general posture that an explicit
caller input never disappears without saying so (the same instinct behind KI-083/KI-089's
existence checks, KI-049's literal-content validation, and SPEC §10.3's "never silently overwritten"
extended in spirit). A caller passing `supersedes=` on a `static` or `cardinality="single"` property
has almost certainly misunderstood what it does; a loud `ValidationError` catches that immediately
instead of the hint quietly doing nothing.

**Why the membership check lives in `route()`, not `Ontology`:** `route()` already receives the
authoritative, freshly-fetched `existing` candidate set (`_apply_with_conflict_routing` fetches it
inside the write transaction) and already computes the exact overlapping-and-differing filter the
hint needs to be checked against — duplicating that filter in `Ontology` would risk the two
computations drifting apart. `route()` stays pure (no I/O — `existing` is already provided) and
raises a plain `ValueError`, the same precedent `govern/policy.py`'s constructors already set for
domain-layer contract violations; `Ontology` is the one place that translates it into the stable,
interface-facing `ValidationError` taxonomy (SPEC §16), consistent with every other `OntolithError`
in this codebase originating at the `Ontology` boundary, not inside `govern/`.

**Why not a `resolve_supersession()`-style separate call instead of a write-time parameter:**
rejected — SPEC §10.2's supersession is meant to happen inline with the write that causes it ("the
world changed" is asserted, not separately confirmed), matching how `single`-cardinality
`time_varying` supersession already works with zero extra calls. A separate resolution step would
also reopen a window between "the replacement value is asserted" and "the old value's window is
closed" where a concurrent reader could see both as simultaneously active with no way to tell that
was about to change — the append-only, one-transaction discipline `_apply_with_conflict_routing`
already provides avoids that entirely.

## Consequences

**Positive:**
- Closes a real, reproducible gap: `cardinality="many"` `time_varying` properties now behave as
  their SPEC §4 declaration promises — concurrent, genuinely-distinct values coexist instead of
  silently colliding.
- `cardinality="single"` `time_varying` properties are completely unaffected — same routing, same
  behavior, `supersedes_hint` not even consulted on that branch.
- `static` properties (`single` or `many`) are completely unaffected — `_route_static` never
  receives `supersedes_hint` at all.

**Negative / follow-ups:**
- **Breaking**: an existing `cardinality="many"` `time_varying` property whose callers relied on the
  pre-ADR-0050 unconditional-supersession behavior (e.g. to correct a value by simply asserting a
  new one, with no `supersedes=`) now gets coexistence instead — that caller must add
  `supersedes=<id>` to keep replacing rather than accumulating. `govern.conflict.route()` gains a
  new keyword parameter (`supersedes_hint`, default `None`, appended after every existing parameter
  with a default — every other call shape is unaffected); `_route_time_varying()` (private, no
  external callers) gains both `cardinality` and `supersedes_hint`, since it previously took neither;
  `Ontology.assert_literal()`/`assert_ref()`/`propose()`/`propose_ref()` gain a new keyword-only
  `supersedes` parameter (default `None`) — additive, not breaking, for every other existing caller.
- **REST, GraphQL, MCP, and CLI parity is deliberately out of scope for this ADR/KI.** `supersedes`
  is exposed on the Python SDK only (`assert_literal`/`assert_ref`/`propose`/`propose_ref`) —
  matching this project's established pattern of shipping an SDK-first capability and filing
  interface parity as its own follow-up (e.g. KI-081 → KI-094/KI-096 for `.include_flagged()`/
  `.include_history()`). Filed as **KI-099**.
- `resubmit()`'s existing "payload is replayed unedited" limitation now also covers `supersedes` — a
  proposal cannot have its hint corrected after submission any more than its value or window can
  be, consistent with that pre-existing, documented constraint (not a new one this ADR introduces).
  One concrete consequence: if the hinted target leaves the active set some other way (retracted,
  or itself superseded by a different write) while the proposal sits in `require_review`, `route()`'s
  own fresh-`existing` check rejects the accept with a `ValidationError` every time it's retried —
  the proposal becomes permanently un-acceptable as staged, and the reviewer's only recourse is
  `reject_proposal()`, not a fixable `resubmit()`.
- A `many`-cardinality `time_varying` property with a genuinely large number of concurrent values
  (e.g. dozens of simultaneously-held roles) still requires one `supersedes=` write per correction —
  no batch/bulk-supersede mechanism is introduced here. Not a new gap: no bulk-write mechanism exists
  for any other write path in this codebase either.

## Alternatives Considered

**Always coexist for `cardinality="many"` `time_varying`, no new parameter (mirror `_route_static`
exactly):** See Rationale above — rejected because it cannot express "correct one specific
concurrent value" without a separate `retract()` round trip, unlike every other write path's
single-call ergonomics.

**Infer replacement from a "closest matching prior value" heuristic (e.g. same predicate, most
recent `asserted_at`) instead of an explicit hint:** Rejected — SPEC §10 defines conflict routing
with no inference step (the same reasoning ADR-0017 already used to reject inferring "many" from
repeated proposals); an implicit heuristic could silently supersede the wrong concurrent value with
no way for a caller to know it happened differently than intended, worse than either coexisting or
raising.

**A separate `resolve_supersession(assertion_id, superseded_by)`-style call, decoupled from the
write itself:** Rejected — see Rationale above (reopens a window where both values look
simultaneously, indefinitely, active with no signal a replacement was imminent).

**Affirm ADR-0017's original decision and leave `time_varying`+`many` unextended, close KI-080 as
accepted-as-designed:** Considered as the minimal-change option. Rejected because the gap is real
and reproducible (not merely a documentation nit) and the schema-declared `cardinality="many"`
contract is silently unmet for `time_varying` properties specifically — the same category of gap
ADR-0017 itself was written to close for `static` properties.

## References

- SPEC §4 (Schema — cardinality), §10.1/§10.2 (Conflict handling — routing, temporal supersession)
- ADR-0017 (Cardinality-Aware Conflict Routing for Static Properties — amended by this ADR)
- ADR-0004 (Confidence Semantics — no auto-combine, corroboration precedent)
- `src/ontolith/govern/conflict.py` (`route`, `_route_time_varying`)
- `src/ontolith/ontology.py` (`_require_valid_supersedes_hint`, `_apply_with_conflict_routing`,
  `assert_literal`, `assert_ref`, `propose`, `propose_ref`, `_replay_proposal_operations`)
