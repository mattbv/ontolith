# ADR-0048: `.include_flagged()` / `.include_history()` Are Match-Set Wideners, Not Result-Shape Changes

**Status**: Accepted

**Date**: 2026-09-10

**Deciders**: Ontolith Core Team

**Related**: SPEC §11.2 (symbolic query semantics — names both opt-ins), SPEC §10.3 (a flagged
assertion MUST be excluded from default retrieval unless explicitly requested), ADR-0027 (prior
`QueryBuilder` scope decision — deferred multi-hop traversal), `docs/known-issues.md` KI-081
(closed by this ADR)

---

## Context

SPEC §11.2 names `.include_flagged()` and `.include_history()` as `QueryBuilder` opt-ins ("Flagged/
superseded/retracted assertions are excluded by default; `.include_flagged()` / `.include_history()`
opt in"), but neither method existed on `QueryBuilder`, and its `_base_candidates()` never passed
`entities_where()`'s existing `include_flagged` parameter through (KI-081). Both backends'
`entities_where()` also only consulted `include_flagged` inside their `as_of_time` branch — a
current-state (`kb.query(Person).where(...)`) query hard-coded `status = 'active'`.

The exclusion half of SPEC §10.3's MUST was already satisfied; the *opt-in* half was reachable only
via lower-level SDK calls (`kb.assertions(status="flagged")`, `kb.contradictions()`), never through
the primary fluent API.

Two shapes `.include_history()` in particular could take:

1. **A match-set widener** — `.where()` also matches assertions in non-`active` statuses; the
   result is still `list[Entity]`.
2. **A result-shape change** — the query returns each matched entity *together with* its assertion
   timeline (a new return type / method).

## Decision

**Both `.include_flagged()` and `.include_history()` are match-set wideners.** They change only
*which assertion statuses a `.where()` filter is allowed to match against*. `.all()` /
`.first()` / `.count()` keep returning `Entity` / `Entity | None` / `int` — no timeline is
attached, no new return type is introduced.

| Method                 | Widens the current-state match set to include |
|------------------------|-----------------------------------------------|
| *(default)*            | `active`                                       |
| `.include_flagged()`   | `active` + `flagged`                           |
| `.include_history()`   | `active` + `superseded` + `retracted`          |
| both                   | every status                                  |

**The two flags are independent.** `.include_flagged()` does not pull in `superseded`/`retracted`,
and `.include_history()` does not pull in `flagged` — each opt-in is scoped to exactly the statuses
its name implies.

**No effect without a `.where()` filter.** `kb.query(Person).include_flagged().all()` returns every
`Person` entity, same as `kb.query(Person).all()` — there is no predicate to match, so there is
nothing to widen. (`_base_candidates()` calls `entities()`, not `entities_where()`, when there are
no filters.)

**No effect under `.as_of(t)`.** A bitemporal snapshot matches whatever assertion's validity window
covered `t`, regardless of that assertion's status *now*, so `superseded`/`retracted` history is
already visible there by construction — `.include_history()` is a no-op on the `as_of` path.
`.include_flagged()` *does* still apply under `.as_of()` (a flagged assertion has an open window),
and the `as_of` path has honored `entities_where(include_flagged=...)` since before this ADR.

**Implementation.** `entities_where()` gains an `include_history: bool = False` parameter on the
`StorageBackend` port and both adapters. The current-state branch replaces its hard-coded
`" AND status = 'active'"` with `" AND status IN (?, …)"`, parameter-bound from the widened status
list. `QueryBuilder` gains `_include_flagged` / `_include_history` fields, the two chainable
methods, and threads both through to `entities_where()` from `_base_candidates()` and
`_semantic_candidates()`.

## Rationale

- **The widener form is what SPEC §11.2's own sentence describes** — "assertions are excluded by
  default; … opt in" is about *visibility of assertions to a filter*, not about changing what a
  query returns. The result-shape form would be a different feature the spec doesn't sketch.
- **Symmetry with what already exists.** `entities_where(include_flagged=...)` and
  `assertions(status=...)` are already status-set controls; this extends the same idea to the
  fluent API rather than inventing a parallel mechanism.
- **Cheap and contained.** One new bool on one port method, a widened `IN` clause on two adapters,
  two `QueryBuilder` methods. No new return type, no `govern/` change, no conflict-routing change.
- **`.include_history()` splitting `superseded`+`retracted` from `flagged`** matches how the two
  arise: supersession/retraction is the normal passage of time, flagging is an unresolved
  disagreement (SPEC §10). A caller asking for history usually does not also want contradicted
  values mixed in, and vice versa.

## Consequences

**Positive:**
- SPEC §10.3's opt-in half is now reachable through the primary `kb.query(Concept)` API.
- No public return-type change; `.include_*()` compose with `.where()` / `.semantic()` /
  `.min_confidence()` / `.limit()` like any other builder method.
- The `as_of` path's pre-existing `include_flagged` handling is unified with the current-state path
  under one parameter set.

**Negative:**
- A caller wanting an entity's actual assertion *timeline* still uses `kb.assertions(subject=…,
  status=None)` or `kb.provenance()` (ADR-0047) — `.include_history()` deliberately does not
  provide it. If a timeline-returning query is wanted later, it is a separate method and a separate
  decision.
- `.include_flagged()` / `.include_history()` silently do nothing on a filter-less or `as_of`
  query. Documented on each method; not surfaced as a warning (consistent with the builder's other
  no-op combinations, e.g. `.limit()` larger than the result set).

## Alternatives Considered

**`.include_history()` returns per-entity timelines.** Rejected — a bigger surface (new return
type) than SPEC §11.2 sketches, and `kb.assertions(status=None)` / `kb.provenance()` already cover
the "give me the history" need.

**One combined `.include_all_statuses()` flag instead of two.** Rejected — SPEC §11.2 names both
methods explicitly, and the flagged-vs-history split is a meaningful distinction (disagreement vs
passage of time) a caller will often want to make.

**Defer `.include_history()` with an ADR (mirroring ADR-0027's multi-hop deferral), ship only
`.include_flagged()`.** Rejected — the widener form makes `.include_history()` nearly free (two
extra status strings in the same `IN` clause), so there is no implementation cost worth deferring.
