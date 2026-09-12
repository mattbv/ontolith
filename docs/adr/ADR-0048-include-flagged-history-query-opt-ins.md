# ADR-0048: `.include_flagged()` / `.include_history()` Are Match-Set Wideners, Not Result-Shape Changes

**Status**: Accepted

**Date**: 2026-09-10

**Deciders**: Ontolith Core Team

**Related**: SPEC §11.2 (symbolic query semantics — names both opt-ins), SPEC §10.3 (a flagged
assertion MUST be excluded from default retrieval unless explicitly requested), ADR-0027 (prior
`QueryBuilder` scope decision — deferred multi-hop traversal), `docs/known-issues.md` KI-081
(closed by this ADR), KI-093 (confidence/trust composition, closed by this ADR's own Update)

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

**No effect with neither a `.where()` filter nor a confidence/trust floor.**
`kb.query(Person).include_flagged().all()` returns every `Person` entity, same as
`kb.query(Person).all()` — there is no predicate to match, so there is nothing to widen
(`_base_candidates()` calls `entities()`, not `entities_where()`, when there are no filters). This
is narrower than "no effect without `.where()`": since KI-093, `.min_confidence()`/
`.trust_at_least()` are evaluated by `_apply_confidence_trust_filters()` regardless of whether
`.where()` was called, so `kb.query(Person).min_confidence(0.5).include_history()` *does* differ
from `kb.query(Person).min_confidence(0.5)` even with no `.where()` in sight.

**`.include_history()` is a no-op under `.as_of(t)`.** The `as_of` branch never restricts matches
to `active` in the first place — it excludes only `flagged` (gated behind `.include_flagged()`,
which *does* still apply under `.as_of()` — a flagged assertion has an open window, and the
`as_of` path has honored `entities_where(include_flagged=...)` since before this ADR) — so a
`superseded` value is already visible there whenever `t` predates its supersession, with no
opt-in needed.

This leaves one `as_of` gap this ADR does **not** fix: a `retract()` does not always narrow an
already-open validity window (`_retraction_valid_to`), so a retracted assertion with a future
`valid_to` still matches an `.as_of(t)` snapshot for `t` after the retraction, with no way to
exclude it — SPEC §11.2's "retracted excluded by default" is unmet on that path. Pre-existing (the
`as_of` branch has always been window-based, not status-based); filed as KI-095, not addressed
here.

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
- No public return-type change; `.include_*()` compose with `.where()`, `.semantic()`,
  `.min_confidence()`, `.trust_at_least()`, and `.limit()` like any other builder method (the
  confidence/trust composition shipped in KI-093, see Update below).
- The `as_of` path's pre-existing `include_flagged` handling is unified with the current-state path
  under one parameter set.

**Negative:**
- A caller wanting an entity's actual assertion *timeline* still uses `kb.assertions(subject=…,
  status=None)` or `kb.provenance()` (ADR-0047) — `.include_history()` deliberately does not
  provide it. If a timeline-returning query is wanted later, it is a separate method and a separate
  decision.
- `.include_flagged()` / `.include_history()` silently do nothing on a query with neither a
  `.where()` filter nor a confidence/trust floor, and `.include_history()` specifically is also a
  no-op under `.as_of()`. Documented on each method; not surfaced as a warning (consistent with the
  builder's other no-op combinations, e.g. `.limit()` larger than the result set).

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

## Update (2026-09-11, closes KI-093): the wideners now compose with `.min_confidence()`/`.trust_at_least()`

Filed while reviewing this ADR's own first implementation: `.include_flagged()`/`.include_history()`
widened `_base_candidates()`'s `entities_where()` call, but `_apply_confidence_trust_filters()` —
run afterward for `.min_confidence()`/`.trust_at_least()` — called `entities_meeting_confidence()`/
`entities_meeting_trust()`, whose current-state branches still hard-coded `a.status = 'active'`. So
a supposedly-no-op floor (`min_confidence(0.0)`, `trust_at_least(0)`) chained after
`.include_history()` re-narrowed the result back to active-only and could empty it — the
composition claim this ADR made at first was false.

**Fix:** `entities_meeting_confidence()`/`entities_meeting_trust()` (port + both adapters) gained
the identical `include_flagged`/`include_history` parameters `entities_where()` already has, and
their current-state branch was widened the same way: `a.status = 'active'` became a parameter-bound
`a.status IN (?, …)` built from the same status list. Their `as_of` branches gained the identical
`flagged_clause` treatment `entities_where()`'s `as_of` branch already had (`include_flagged`
applies, `include_history` is a no-op — same reasoning). `QueryBuilder._apply_confidence_trust_filters`
threads `self._include_flagged`/`self._include_history` into both calls. No semantics question to
resolve beyond "match the existing widener": an entity that *historically* had a
≥threshold-confidence (or ≥threshold-trust) assertion now qualifies under `.include_history()`,
consistent with `.where()`'s own widened matching.

New tests: `test_query.py::TestIncludeFlaggedHistoryComposesWithConfidenceAndTrust` (all four
`{include_flagged, include_history} x {min_confidence, trust_at_least}` combinations — a genuine
no-op floor doesn't re-narrow; a genuinely-failing floor still excludes, even widened);
`test_sqlite_backend.py`/`test_duckdb_backend.py::test_entities_meeting_{confidence,trust}_status_widening_flags`
at the port level; `conformance/test_include_flagged_history.py::TestComposesWithConfidenceAndTrust`
(both backends). A review round found the `as_of` branch's `include_flagged` handling was
untested on both new methods (a mutation reverting `flagged_clause` to unconditional exclusion
survived the whole suite) — closed with `test_entities_meeting_{confidence,trust}_as_of_include_flagged`
at the port level and `conformance/test_confidence_trust_filters.py`'s
`test_{trust_at_least,min_confidence}_as_of_include_flagged_opts_back_in`, both backends, all
mutation-tested. The same round also caught that this ADR's own "No effect without a `.where()`
filter" claim (Decision section, above) had quietly become false: `.min_confidence()`/
`.trust_at_least()` are evaluated regardless of whether `.where()` was called, so
`kb.query(Person).min_confidence(0.5).include_history()` now differs from
`kb.query(Person).min_confidence(0.5)` with no `.where()` in sight — corrected there, in
`QueryBuilder.include_flagged()`/`.include_history()`'s docstrings, in `docs/adr/README.md`'s
index entry, and in SPEC §11.2.

A second review round found one more instance of the exact same class of gap, in code this KI
didn't touch: `_semantic_candidates()` (KI-081) also threads `include_history` into its own
`entities_where()` call, and that threading was equally untested (hardcoding it to `False`
survived the whole suite too) — closed with `test_semantic_where_honors_include_history`,
mirroring the `include_flagged` case KI-081 already had, mutation-tested. Also added a pinning
test for `include_history`'s documented `as_of` no-op, and found that a `# nosec B608` on
`entities_where()`'s own `as_of` `flagged_clause` line was equally dead (removing it doesn't
change bandit's finding count) — all such dead markers, old and new, removed; verified
bandit-clean throughout.

## Update (2026-09-12, closes KI-094): the two opt-ins now reach REST, GraphQL, and MCP

This ADR's opt-ins were reachable only from the Python SDK — the three interface `query`
surfaces (`interfaces/rest.py`'s `/query`, `interfaces/graphql.py`'s `Query.query`,
`interfaces/mcp.py`'s `ontolith.query`) forwarded `semantic`/`min_confidence`/`trust_at_least`/
`limit` but not `include_flagged`/`include_history`. Closed mechanically: each interface gained
the two booleans (default `False`, matching the SDK default), forwarded to the builder the same
way the existing four are. GraphQL's `strawberry` layer camelCases them to `includeFlagged`/
`includeHistory` on the wire. No semantics changed — this is wiring, not a new decision.

New tests, two per interface (`include_flagged` against a flagged assertion, `include_history`
against a retracted one, each with a false-default baseline) plus one MCP-specific test
combining `include_flagged` with `as_of` (the one branch where the interface-level wiring is
genuinely distinct, since MCP alone builds from `kb.as_of(...)` rather than `kb.query(...)`),
all mutation-tested by reverting each interface's `if include_flagged: ... if include_history:
...` wiring in turn.

A review round found GraphQL's `run_in_threadpool(_execute_query, ...)` call site passed all
nine arguments positionally, including two adjacent `bool` and two adjacent `int | None`
parameters mypy's ParamSpec check can't catch a silent reorder of — switched to keyword
arguments (`run_in_threadpool` has no other call site in this codebase; REST and MCP avoid the
hazard structurally instead, since neither uses a threadpool call here — REST reads `body.<field>`
attributes directly in a sync route, MCP chains `QueryBuilder` methods inline). The same round
corrected this ADR's own and KI-094's filed claim that REST/GraphQL "already forward `as_of`":
only MCP does; REST and GraphQL's `query` surfaces have no `as_of` parameter at all.

It also found the new MCP docstring's `include_history`+`as_of` no-op explanation repeated a
pre-existing inaccuracy this ADR's own Decision section (above) and every other copy of the same
note (`QueryBuilder.include_history()`, `StorageBackend.entities_where()`/
`entities_meeting_confidence()`, both adapters, KI-081's own known-issues.md entry) shared: "a
bitemporal snapshot already matches whatever was valid at that instant regardless of status now"
is not quite true — the `as_of` branch does consult status for one case (`flagged` is excluded
unless `include_flagged` is set, per `entities_meeting_confidence()`'s own "One caveat" paragraph
two sections up). A first attempted reword landed on the MCP copy alone and, in trying to avoid
that inaccuracy, introduced a different wrong claim ("matches purely on the validity window, not
on current `status`") that a second review round caught by cross-checking it against the adapters'
own SQL (`flagged_clause`) and the sibling `include_flagged` no-op-under-`as_of` claim six lines
above it in the same docstring. Corrected everywhere with the same precise framing: the `as_of`
branch never restricts matches to `active` — it excludes only `flagged` — so `superseded`/
`retracted` assertions whose window covers the queried instant already match without
`include_history`; the KI-095 pointer (a *different* gap — no way to exclude a stale-window
retraction from `as_of`) was already correct in the first reword and is kept.
