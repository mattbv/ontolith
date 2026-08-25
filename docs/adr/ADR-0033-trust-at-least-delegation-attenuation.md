# ADR-0033: `.trust_at_least()` Compares Effective (Delegation-Attenuated) Trust, Not the Author's Raw `trust_level`

**Status**: Accepted
**Date**: 2026-08-24
**Deciders**: Ontolith Core Team
**Related**: SPEC §8.4 (effective capability under delegation), `govern/policy.py`'s `ThresholdPolicy.evaluate()`, ADR-0020 §6 (`.trust_at_least()`'s original semantics, amended by this ADR), KI-028 (push-down design), KI-036 (bitemporal `as_of` scoping), KI-037 (`candidate_ids` narrowing), KI-039 (prior cross-backend `min`/`least`-shaped SQL divergence), KI-047

---

## Context

`QueryBuilder.trust_at_least(level)` is backed by `StorageBackend.entities_meeting_trust()`, a single push-down SQL query (KI-028) that joins `assertion.author = principal.id` and compares `principal.trust_level >= min_trust` directly. But an assertion made under delegation (`acting_as` set) doesn't get its trust taken at face value elsewhere in this codebase: `govern/policy.py`'s `ThresholdPolicy.evaluate()` computes `trust_level = min(principal.trust_level, acting_as.trust_level)` before comparing against a proposal's threshold — an *effective* trust that can be no higher than the lower of the two principals involved. `entities_meeting_trust` never read `acting_as` at all, so `.trust_at_least()` could disagree with the policy engine about the trust of the very same assertion: a low-trust delegate acting as a high-trust principal was scored as fully trusted by the query filter, though the policy engine that decided whether to auto-accept that same assertion would have scored it lower; a high-trust delegate acting as a low-trust principal was scored as low-trust by the query filter even though SPEC's effective-value rule agrees with that direction.

SPEC §8.4 states this `min()` rule explicitly only for **capability** ("the effective capability for the operation is `min(capability(author), capability(acting_as))`"). It says nothing about trust. `govern/policy.py`'s trust-min is Ontolith's own conservative extension of that same principle — applied because a policy threshold gated on trust has the identical laundering concern a capability check does (a low-trust principal delegating to/through a high-trust one shouldn't thereby earn a trust score it didn't independently establish) — not a separate SPEC mandate. This ADR's fix extends that same *codebase-established* formula to `.trust_at_least()`, by analogy with SPEC §8.4's capability rule, the same way `govern/policy.py` already does; it does not claim SPEC directly requires it for trust.

Found during KI-036's review while double-checking the "trust_level is exact, not an approximation" claim that fix's docstrings make — true for whether `trust_level` needs bitemporal reconstruction, but that claim didn't cover this separate gap in which column the query reads.

## Decision

`entities_meeting_trust` (`store/sqlite/backend.py`, `store/duckdb/backend.py`) now `LEFT JOIN`s a second `principal` alias (`delegate`) on `assertion.acting_as = delegate.id`, and filters on the effective-trust formula instead of the author's raw `trust_level`:

```sql
-- SQLite
... LEFT JOIN principal delegate ON delegate.id = a.acting_as
WHERE ... AND min(p.trust_level, coalesce(delegate.trust_level, p.trust_level)) >= ?

-- DuckDB
... LEFT JOIN principal delegate ON delegate.id = a.acting_as
WHERE ... AND least(p.trust_level, coalesce(delegate.trust_level, p.trust_level)) >= ?
```

No `acting_as IS NULL` special case is needed: `coalesce` falls back to the author's own `trust_level` when there's no delegate, and `min(x, x) == x`, so a non-delegated assertion (or the `_resolve_delegation`-recognized self-delegation no-op, `acting_as == author`) is scored identically to before.

**Cross-backend divergence**, verified empirically before implementing (same pattern as KI-039's `TRY_CAST`/`CAST` split): SQLite's `min(a, b)` is the scalar two-argument form, but DuckDB's `min(a, b)` is aggregate-only and returns a *list* when given two scalar arguments (`duckdb min(3,5)` → `[3]`, not `3`). DuckDB's scalar two-arg minimum is `least(a, b)` (`duckdb least(3,5)` → `3`). `min()`/`least()` are not universally equivalent — `sqlite min(3, NULL)` is `NULL` (excludes the row) while `duckdb least(3, NULL)` is `3` (includes it) — but this can't currently fire: `principal.trust_level` is `INTEGER NOT NULL` on both backends' schemas, and `coalesce` already resolves the no-delegate case before either function sees a NULL. Both backend docstrings/comments name this dependency explicitly so a future relaxation of that column constraint doesn't silently diverge the two backends in opposite directions.

**Dangling `acting_as` fails open, not closed** — a deliberate, documented asymmetry with `govern/policy.py`. There is no FK from `assertion.acting_as` to `principal.id` on either backend, so an assertion can in principle reference a delegate that no longer resolves (unreachable through `Ontology`'s own write paths today — `_resolve_delegation` always validates the delegate exists before an assertion can be created with that `acting_as` — but reachable via a direct `StorageBackend.put_assertion()` call, e.g. a legacy import bypassing `Ontology`). `entities_meeting_trust`'s `LEFT JOIN` + `coalesce` falls back to the author's own `trust_level` for such a row rather than excluding it or raising. This is the opposite of `_resolve_delegation`'s own behavior for the same input (`AuthError: Delegating principal not found`), and that's intentional: policy evaluation runs once, at write time, when failing loudly and rejecting the write is cheap and correct; `entities_meeting_trust` runs on every query against already-committed data, where excluding or erroring on a row for data that was already accepted would be a surprising, un-auditable behavior change with no corresponding write to explain it. `StorageBackend.entities_meeting_trust`'s Protocol docstring states this fallback as a normative part of the port contract (not just an implementation detail), and a conformance vector (`TestTrustAtLeastDelegationAttenuation::test_dangling_acting_as_falls_back_to_author_trust_level`) pins it via a direct `put_assertion()` call, so a third-party backend choosing a different fallback (e.g. an `INNER JOIN` that silently drops the row, or `coalesce(delegate.trust_level, 0)` that fails closed) would fail conformance rather than self-certifying with divergent behavior.

`QueryBuilder.trust_at_least()`'s and `StorageBackend.entities_meeting_trust`'s docstrings now describe "effective trust_level" and spell out the delegation formula, rather than implying the author's raw value. New conformance vectors (`conformance/test_confidence_trust_filters.py::TestTrustAtLeastDelegationAttenuation`) cover: both attenuation directions (low-trust delegate acting as a high-trust principal, and the reverse — the direction that actually discriminates "reads effective trust" from "reads only the author's own raw `trust_level`", since the other direction's own delegate trust_level already sits below the tested threshold either way); the inclusive threshold boundary, deliberately oriented as a high-trust delegate acting as a mid-trust principal (not the reverse, which would coincide with the delegate's own raw value at that boundary and fail to discriminate); non-delegated assertions being unaffected by the new join; and the dangling-delegate fallback above.

## Consequences

**Positive:**
- Closes KI-047: `.trust_at_least()` and `govern/policy.py`'s auto-accept decision now agree on the effective trust of the same delegated assertion.
- No new SQL infrastructure — the `LEFT JOIN`/`coalesce` shape mirrors patterns already used elsewhere in these backends; the `min`/`least` split follows KI-039's already-established precedent for handling this exact category of cross-backend SQL-function divergence.

**Negative / follow-ups:**
- **Breaking (observable, not signature-level):** any caller relying on `.trust_at_least()` matching a delegated assertion by the author's raw `trust_level` alone will now see it filtered by the (possibly lower) effective value instead. Flagged as `**Breaking:**` in CHANGELOG per ADR-0019's policy, matching KI-043/KI-044's precedent of calling out behavior-level breaks even without a signature change. Non-delegated assertions (the common case) are unaffected.
- The dangling-`acting_as` fail-open fallback is permissive by design (see Decision above) but means a backend row corrupted or hand-inserted with a bogus `acting_as` is scored at the author's trust rather than flagged as suspicious in any way — accepted as out of scope for this fix; nothing in the codebase currently produces such a row through a governed path.
- One extra `principal` PK probe per scanned assertion row from the new join; negligible against the SPEC-budgeted hybrid-query latency (`principal.id` is indexed as the primary key on both backends), and `trust_at_least` was already O(1) round-trips before this change (KI-028) — this doesn't change that shape.

## Alternatives Considered

- **`CASE WHEN a.acting_as IS NOT NULL THEN min(...) ELSE p.trust_level END`** instead of `LEFT JOIN` + `coalesce`: functionally equivalent for every case exercised, but more verbose and requires the same `min`/`least` split duplicated inside both branches. `coalesce` centralizes the "no delegate" fallback in one place and reads closer to `govern/policy.py`'s own `if acting_as is not None: ...` shape (attenuate, otherwise leave the raw value as-is).
- **Failing closed on a dangling `acting_as`** (excluding the row, or raising), matching `_resolve_delegation`'s own behavior for the identical input: rejected — see the Decision section's rationale for why write-time and query-time failure semantics for this specific input are deliberately asymmetric.
- **Not fixing this at all, treating the disagreement as acceptable**: rejected — the whole point of `.trust_at_least()` existing as a query-time trust filter is to let callers reason about the same effective-trust concept the policy engine already enforces at write time; a silent, undocumented divergence between the two undermines that guarantee for any deployment using delegation.
