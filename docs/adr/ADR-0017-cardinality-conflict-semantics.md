# ADR-0017: Cardinality-Aware Conflict Routing for Static Properties

**Status**: Accepted
**Date**: 2026-07-14
**Deciders**: Ontolith Core Team
**Related**: ADR-0005 (Conflict Model), SPEC §4 (Schema), SPEC §10 (Conflict handling), `.claude/rules/conflict.md`

---

## Context

`PropertyDef.cardinality` (`"single"` | `"many"`, default `"single"`) is declared in the schema IR (`src/ontolith/schema/ir.py`) but never consulted anywhere in conflict routing (`src/ontolith/govern/conflict.py`) or the write path (`src/ontolith/ontology.py`). A `cardinality="many"` property — e.g. a person's phone numbers, a paper's co-authors — receives identical treatment to `cardinality="single"` today: any two differing static values on the same (subject, predicate) are flagged as a contradiction and routed to review, even when the property is explicitly schema-declared as legitimately multi-valued.

SPEC §10 defines routing purely by *temporality* (`static` → contradiction, `time_varying` → supersession) and is silent on cardinality's role. Surfaced during a whole-project audit (2026-07-14): this is a real gap, not an oversight to leave alone — a schema author declaring `cardinality="many"` has no way to express that intent to the conflict-routing layer, so every `many`-cardinality property behaves exactly like a `single`-cardinality one and floods review queues with false contradictions for facts that were never in dispute.

## Decision

Extend `_route_static` (and the public `route()` entry point) with a `cardinality: Literal["single", "many"]` parameter, defaulting to `"single"` (matching the schema default, so untouched call sites keep today's behavior):

- `cardinality="single"` (default): unchanged — any differing value on an overlapping window contradicts (SPEC §10.3, ADR-0005).
- `cardinality="many"`: a differing value on an overlapping window **activates** (coexists) instead of contradicting. Corroboration (same value, multiple sources) already activates under the existing rule; this extends "no conflict" to *distinct but simultaneously-true* values for properties the schema declares as legitimately multi-valued.

`static` + `cardinality="many"` is now a third first-class routing outcome, distinct from both `static` + `single` (contradiction) and `time_varying` (supersession):

| temporality    | cardinality | differing value behavior              |
|----------------|-------------|----------------------------------------|
| `static`       | `single`    | Contradict (SPEC §10.3, unchanged)     |
| `static`       | `many`      | Activate — coexist (this ADR)          |
| `time_varying` | (either)    | Supersede if windows overlap (§10.2)   |

`Ontology._apply_with_conflict_routing` resolves `cardinality` from the active schema (via `SchemaIR`, mirroring how `temporality` is already resolved) and passes it to `route()`.

This ADR also folds in the companion fix needed to make the SPEC's own §10.1 formula ("`existing` := active assertions... whose validity **overlaps** A") apply to the static branch, which it currently doesn't: `_route_static` gains the same `_windows_overlap` filter `_route_time_varying` already has. This is a pure bug fix (the SPEC text already says overlap applies to both branches; only the `time_varying` branch implemented it) bundled here because it touches the same function in the same PR — it is not itself a new design decision.

## Rationale

**Why "many coexists, single contradicts" and not some other rule:**
- Matches real-world semantics: a `cardinality="many"` field describes a genuinely multi-valued fact. Two different phone numbers for the same person aren't in dispute — they're both true. Routing them to review as a "contradiction" is a false positive that erodes trust in the review queue (SPEC §10.3's entire premise is that contradictions are unexpected).
- Consistent with the existing corroboration rule: `_route_static` already activates same-value assertions from multiple sources without merging confidence (ADR-0004). Extending "activate" to distinct-but-compatible values for `many`-cardinality properties is the same instinct — don't force a decision where none is needed — applied one step further, gated by an explicit schema declaration rather than inferred.
- Keeps the default (`single`) behavior byte-for-byte unchanged. No existing schema, test, or conformance vector declares `cardinality="many"` today (confirmed: zero non-schema/codegen references to `cardinality` anywhere in `src/`), so this is purely additive.

**Why gate on cardinality rather than trying to infer multi-valuedness some other way:**
- The schema is the only place this intent can be declared unambiguously. Cardinality already exists as a schema field for exactly this kind of "how many values can this hold" question (SPEC §4); routing consuming it is completing an existing declaration, not inventing a new one.

**Why not extend this to `time_varying` properties:**
- `time_varying` properties already support multiple coexisting values via non-overlapping windows (SPEC §10.2 — e.g. employment history). Cardinality doesn't add anything new there; two *overlapping-window*, differing-value `time_varying` assertions are a genuine supersession regardless of cardinality (the newer one replaces the older), so `cardinality` is not threaded into `_route_time_varying`.

## Consequences

**Positive:**
- Closes a real audit finding: `cardinality="many"` schema declarations now have observable effect.
- No behavior change for any existing schema (default is `single`).
- The bundled window-overlap fix makes `_route_static` match the SPEC's own stated formula, closing a second, related audit finding with the same code change.

**Negative / follow-ups:**
- `entities_where()`/query-layer behavior for multi-valued properties (e.g. "does this Person have phone number X") is unaffected by this ADR — it already returns all active assertions per predicate, which is the correct shape for `many`-cardinality reads. No further query-layer change is needed as a result of this decision.
- `required` (also declared on `PropertyDef`, also currently unconsulted) is explicitly out of scope for this ADR — it's a presence/absence validation concern, not a conflict-routing concern, and is tracked separately.
- **`flag_contradiction()` deliberately does not consult cardinality, by design, not oversight.** It is an explicit reviewer/proposer action ("I believe these specific assertions conflict"), unlike `route()`'s automatic, schema-driven inference. Once a contradiction is open on a `many`-cardinality predicate — whether opened via `flag_contradiction()` or (in principle) surviving from before a schema change — `_apply_with_conflict_routing`'s "extend an already-open contradiction" fast path (SPEC §10.3: "any new incoming assertion must be added to the same contradiction") sweeps in subsequent same-predicate assertions for review, the same as it would for a `single`-cardinality predicate; it does not re-check cardinality and silently `Activate` instead. This is intentional: an explicit human dispute should not be silently overridden by the schema's cardinality default — if a reviewer suspects two phone numbers are actually a duplicate/typo rather than two genuinely distinct facts, routing every subsequent phone assertion to review until the dispute is resolved is the conservative, correct behavior, consistent with "static facts are never silently overwritten" extended to "an open dispute is never silently un-disputed." A schema author who wants `many`-cardinality coexistence to be un-overridable by explicit flagging would need a different mechanism than this ADR provides — not addressed here.

## Alternatives Considered

**Infer "many" from repeated proposals rather than requiring an explicit schema declaration:** Rejected — SPEC §10 defines conflict routing precisely by temporality, with no inference step; adding an implicit heuristic on top would introduce behavior the SPEC doesn't specify. An explicit, already-existing schema field is the correct signal instead.

**Treat `many`-cardinality differing values as corroboration (merge into one record):** Rejected — corroboration in this codebase means *the same value* from multiple sources, kept separate without confidence-merging (ADR-0004). Two *different* phone numbers are not corroborating each other; they're two independently true facts. Activating both as separate assertions (not merging) is the correct shape.

## References

- SPEC §4 (Schema — cardinality), §10 (Conflict handling)
- ADR-0004 (Confidence Semantics — no auto-combine), ADR-0005 (Conflict Model)
- `.claude/rules/conflict.md` (routing pseudocode this ADR extends)
- `src/ontolith/schema/ir.py` (`PropertyDef.cardinality`)
- `src/ontolith/govern/conflict.py` (`_route_static`, `_windows_overlap`)
