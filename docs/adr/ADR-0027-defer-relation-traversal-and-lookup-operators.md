# ADR-0027: Defer Multi-Hop Relation Traversal and Lookup Operators in `QueryBuilder.where()`

**Status**: Accepted (amended — see Amendment below)

**Date**: 2026-08-03 (amended 2026-08-12 — lookup operators implemented, KI-039)

**Deciders**: Ontolith Core Team

**Related**: SPEC §11.1 (Query builder), `docs/known-issues.md` KI-030, KI-039

---

## Context

SPEC §11.1 labels `kb.query(Person).where(employer__name="Analytical Engine Co.")` the
"normative shape" of the query builder, using Django-style double-underscore ("dunder")
syntax to imply multi-hop traversal: filter `Person` by a property (`name`) on the entity
its `employer` relation points to. `QueryBuilder.where()` never implemented this. Instead,
`_qualified_filters()` compiled *any* keyword argument into the literal predicate string
`f"{concept}.{key}"` — so `employer__name` became the predicate `"Person.employer__name"`,
which can never match anything. `.where()` accepted this silently and returned an empty
list, no error, no warning (KI-030).

A related, separately-documented example (`docs/Ontolith_UseCases_and_Interfaces.md` §4.3)
used `text__contains="Compound X"` — a substring lookup operator, a different but
similarly-shaped gap: no lookup-operator syntax of any kind is implemented either, only
equality (tracked as KI-039, not fixed by this ADR).

KI-030's own fix text offered two paths: reject the syntax outright with a clear error, or
defer it via an ADR if the gap is deliberate rather than simply unbuilt. A prior pass
(`fix(query-store): ...`, KI-030) took the first path — raising `ValidationError` on any
dunder-containing filter key — without recording why multi-hop traversal itself remains
unbuilt two milestones after SPEC §11.1 called it "normative." That left the SPEC and the
code in open contradiction, flagged in review as a Definition-of-Done gap ("ADR
created/updated if a decision changed").

## Decision

Multi-hop relation traversal (`employer__name=`) and lookup operators (`text__contains=`)
are **deliberately deferred**, not simply unbuilt. `.where()`'s contract for the
foreseeable term is: equality-only filtering against the queried concept's own predicates —
literal properties compared against `value_lit`, relations compared against `value_ref`
(the target entity's id). Any filter key containing `__` raises `ValidationError` at call
time, naming the offending key, rather than silently compiling into an unreachable
predicate (KI-030's actual code fix, unchanged by this ADR).

SPEC §11.1's example was amended to the equality form the implementation actually
supports (`where(employer="org-123")`), with an explicit note that multi-hop traversal is
deferred per this ADR. `docs/Ontolith_PRD.md`'s equivalent example and
`docs/Ontolith_UseCases_and_Interfaces.md` §4.3's `__contains` example were both updated to
their working equality-based form.

## Amendment (2026-08-12): Lookup operators implemented (KI-039)

**What changed**: the "lookup operators" half of this ADR's deferral is superseded.
`.where()` now recognizes a closed set of five dunder-suffixed lookup operators —
`__contains` (substring match, `value_lit` only) and `__gt`/`__lt`/`__gte`/`__lte`
(numeric range, restricted to predicates the active schema declares `Integer` or
`Float`) — implemented in `fix/ki-039-where-lookup-operators`. `docs/known-issues.md`
KI-039 is marked resolved; `docs/Ontolith_UseCases_and_Interfaces.md` §4.3's example
was restored to `where(text__contains="Compound X")`.

**What has NOT changed**: multi-hop relation traversal (`employer__name=`) remains
deferred, unimplemented, and still rejected with `ValidationError` — this ADR's
Decision, Rationale (the join/cardinality/bitemporal design surface), and
Alternatives Considered all still hold for that half. A dunder key is now routed one
of three ways: a recognized operator suffix (implemented), a second `__` in the
remaining prefix (still rejected — that shape is what multi-hop traversal would use),
or an unrecognized suffix (still rejected). `ValidationError`'s message was updated to
name the actual closed operator set rather than claiming, as it did before this
amendment, that no dunder syntax of any kind works.

**Why amend rather than supersede**: only one of the two deferred features
(lookup operators) shipped; the traversal half of the original decision, its
rationale, and its alternatives-considered analysis are all still the current,
correct record — writing a new ADR to replace this one whole would either duplicate
that unchanged material or leave it undocumented. SPEC §11.1 and `docs/Ontolith_SPEC.md`
should be updated to reflect that lookup operators are no longer deferred, in the same
pass that resolves KI-039 (tracked there, not repeated here).

## Rationale

**Why defer rather than implement now:** Multi-hop traversal requires joining through a
second entity's assertions inside `entities_where()`'s SQL (or an equivalent multi-step
resolution), on both backends, plus a decision on how deeply to nest (one hop only, or
arbitrary depth), how to handle a relation with `cardinality="many"`, and how bitemporal
(`as_of`) semantics compose across the join. None of that is a small addition to the
equality-filter code path KI-030 touched; it is a distinct feature with its own design
surface. Building it opportunistically, as a side effect of fixing a silent-no-op bug,
risks picking a shape that doesn't hold up once cardinality/bitemporal composition is
worked through properly.

**Why reject loudly now instead of leaving the silent no-op:** A documented, SPEC-labeled
"normative" example that quietly returns nothing is strictly worse than one that fails
loudly — SPEC §16's own error-taxonomy philosophy (fail fast, machine-readable codes) argues
for the rejection regardless of when traversal itself ships.

**Why an ADR now, on a fix that already shipped:** The KI-030 fix already implemented the
rejection; what was missing was the paper trail explaining that the underlying feature gap
is a deliberate scope decision, not an oversight — and the corresponding SPEC/PRD/use-case
doc updates so those documents stop asserting a contract the code doesn't (and, per this
ADR, currently isn't meant to) honor.

## Consequences

**Positive:**
- SPEC §11.1, the PRD walkthrough, and the use-case doc no longer contradict the shipped
  behavior.
- `.where()`'s actual contract (equality on own properties/relation-target-ids only) is
  explicit and discoverable via `ValidationError` messages that point at this ADR and at
  KI-039, rather than a bare rejection with no further context.

**Negative / follow-ups:**
- Multi-hop traversal remains unimplemented. If/when it's prioritized, it needs its own
  design pass (see Rationale) and should further amend this ADR rather than being bolted
  onto the equality-filter code path.
- Lookup operators (KI-039) were implemented per the 2026-08-12 Amendment above — no
  longer an open follow-up.

## Alternatives Considered

**Implement one-hop traversal only, refuse anything deeper:** Rejected for this pass —
still requires the join-and-cardinality design work called out above; scoping it to
"one hop" doesn't remove the need to decide the cardinality/bitemporal questions correctly,
it just narrows which cases hit them.

**Silently ignore dunder keys (drop them from the filter set) instead of raising:**
Rejected — this is the same silent-failure shape KI-030 exists to close, just relocated
from "matches nothing" to "matches everything" (an ignored filter with no other filters
present returns the unfiltered set) — arguably worse, since it looks like a successful,
unfiltered query rather than an obviously-empty one.

## References

- SPEC §11.1 (Query builder, normative shape)
- `docs/known-issues.md` KI-030 (resolved), KI-039 (lookup operators, resolved 2026-08-12)
- `src/ontolith/query/builder.py` (`QueryBuilder.where()`, `_qualified_filters()`)
- `src/ontolith/store/{sqlite,duckdb}/backend.py` (`entities_where()`)
