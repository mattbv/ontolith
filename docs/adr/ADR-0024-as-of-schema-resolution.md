# ADR-0024: Resolving the Schema Version Effective at `as_of(t)`

**Status:** Accepted

**Date:** 2026-07-24

**Deciders:** Ontolith Core Team

**Related:** SPEC §11.4 ("Schema is resolved to the `schema_version` effective at `t`"),
§19 (informative SHOULD-vector: "`as_of` reconstruction across a schema migration")

## Context

KI-019 documented a gap: SPEC §11.4 states that `as_of(t)` reconstruction resolves "the
schema_version effective at `t`," and §19 names "`as_of` reconstruction across a schema
migration" as a SHOULD-covered vector. Nothing enforced this — `StorageBackend.get_schema(namespace,
version=None)` always returns the latest version, and there was no time-scoped alternative.

Investigating before implementing (rather than acting on the known-issue text as written):
the three schema-consulting helpers the KI's own description named as affected
(`Ontology._resolve_temporality`, `_resolve_cardinality`, `_require_known_predicate`) are called
*only* from write paths — `propose`, `assert_literal`, `assert_ref`, `retract`,
`accept_proposal`. A write happening now is correctly validated and routed against the schema in
force *now* (`accept_proposal` deliberately re-resolves at apply time rather than trusting a
propose-time snapshot — see the CHANGELOG's 2026-07 remediation entries). None of these three
helpers are reachable from `AsOfView` or `QueryBuilder`, both of which are read-only and neither
of which consults schema at all today (confirmed by a full-repository grep for `get_schema`
call sites). So the KI's claim that "`AsOfView` itself" resolves schema at latest was not
accurate against the current codebase — there was no live, reachable bug in returned read
results, only a missing capability: nothing let a caller ask "what was the schema at time `t`"
at all.

Also discovered while investigating: `schema_version`'s storage schema (both backends) already
has an `applied_at TEXT NOT NULL` column, populated via each backend's own injected `Clock`
(`self._clock.now()`) on every `put_schema` call — `SQLiteBackend`/`DuckDBBackend` already
accept a `clock: Clock | None = None` constructor parameter, and `Ontology.connect()` already
threads its own `Clock` into it. This data was already being recorded deterministically; nothing
ever read it back.

## Decision

**1. `SchemaIR` is unchanged.** No new field. The effective timestamp lives only in storage
(`schema_version.applied_at`), not on the domain model — avoids rippling into the YAML
round-trip (`to_yaml`/`from_yaml`, ADR-0013) and class-DSL codegen, neither of which has any
notion of wall-clock apply time today.

**2. `put_schema` is unchanged.** `applied_at` was already being populated correctly and
deterministically (via the backend's injected `Clock`) before this ADR — no new parameter, no
breaking change, no test-call-site migration needed for any of the ~15 existing `put_schema`
callers.

**3. New port method: `StorageBackend.get_schema_at(namespace, at) -> SchemaIR | None`.**
Resolves the highest version whose `applied_at <= at` — the schema in force at `at`, or `None`
if no version had been applied by that time (including a namespace with no schema at all, or
whose first version postdates `at`). Purely additive to the `StorageBackend` Protocol.

**4. New read method: `AsOfView.schema() -> SchemaIR | None`.** Calls
`self._backend.get_schema_at(self._namespace, self._as_of)`. This is the concrete, testable
surface SPEC §11.4's "schema resolved to the version effective at `t`" language describes:
`kb.as_of(t1).schema()` and `kb.as_of(t2).schema()` can now genuinely differ across a migration
boundary. `Ontology.get_schema()`/the existing schema-consulting write helpers are untouched —
they correctly keep resolving latest, since writes are never backdated to a schema version.

**5. `_resolve_temporality`/`_resolve_cardinality`/`_require_known_predicate` are NOT changed.**
They have no `as_of` context to resolve against — they're invoked only mid-write, always at
"now." Threading a time parameter through them would be speculative surface with no reachable
caller; closing KI-019 doesn't require it.

## Rationale

**Why storage-only `applied_at` instead of a `SchemaIR.effective_at` field:** `SchemaIR` is a
`frozen=True` pydantic model that round-trips through YAML and the class DSL — neither format
has (or should have) a notion of "when this was applied to a live KB," since the same `SchemaIR`
value can be authored once and applied to many namespaces/KBs at different times. The apply
timestamp is a fact about *one persistence event*, not about the schema content itself — matches
how `Assertion.asserted_at` (a per-persistence-event fact) is separate from `Assertion.value`
(content).

**Why this wasn't a live read-path bug:** verified directly rather than assumed — a
repository-wide grep for every `get_schema` call site confirmed `AsOfView`/`QueryBuilder` never
call it. The gap was a missing capability (no way to ask for historical schema at all), not
misreported data from an existing call.

## Alternatives Considered

**Add `effective_at` to `SchemaIR` itself, populated via `Clock` in `apply_schema()`,** rejected
per Rationale — wrong layer, and would force `to_yaml`/`from_yaml`/DSL codegen to decide what to
do with a field that has no meaning outside "this one apply event."

**Thread an `as_of: datetime | None` parameter through `_resolve_temporality`/
`_resolve_cardinality`/`_require_known_predicate`,** rejected — no caller today has an `as_of`
context to pass (write paths are always "now"); adding it would be unreachable, untestable
surface with no design justification.

**Do nothing (leave KI-019 open),** rejected once investigation showed the fix was small,
additive, and non-breaking — `applied_at` already existed and only needed a reader.

## Consequences

**Positive:** `kb.as_of(t).schema()` now genuinely resolves the schema effective at `t`,
closing the SPEC §19 SHOULD-vector gap with no breaking change to any existing port method,
model, or test.

**Negative / follow-ups:** none identified — `get_schema_at` has no write-time consumer and
isn't meant to; it exists purely for point-in-time introspection via `AsOfView`.

## References

- SPEC §11.4 (`as_of` temporal query semantics), §19 (informative SHOULD-vector list)
- `docs/known-issues.md` KI-019 (now resolved; the original entry's description of `AsOfView`
  and the write-time helpers as already resolving schema at latest was corrected during
  resolution — see the Context section above)
- ADR-0013 (LinkML-aligned YAML dialect — why `SchemaIR` stays free of a persistence-time field)
