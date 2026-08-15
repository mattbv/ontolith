# ADR-0028: `value_type` Enforced at Core Write Time; `required` Stays Out of Core

**Status**: Accepted
**Date**: 2026-08-04 (amended 2026-08-15 — KI-041/KI-042 resolved)
**Deciders**: Ontolith Core Team
**Related**: SPEC §4 (Schema), SPEC §13.2 (Validator plugins), ADR-0017 (Cardinality-Aware Conflict Routing), ADR-0029 (Validator invocation — resolves KI-041/KI-042), KI-010 (reference plugins), KI-031, KI-040, KI-041, KI-042

---

## Context

`PropertyDef` (`src/ontolith/schema/ir.py`) declares two constraints beyond temporality/cardinality: `value_type` (e.g. `Integer`, `Text`, `Date`) and `required`. Neither was consulted anywhere on the write path. `Ontology._require_known_predicate` rejected an *unknown* predicate but not a *mistyped* one: `assert_literal(..., predicate="Person.age", value_type="Text", ...)` against a predicate declared `value_type: Integer` was silently accepted.

ADR-0017 already flagged the `required` half of this gap while scoping cardinality enforcement, explicitly deferring it: *"`required`... is explicitly out of scope for this ADR — it's a presence/absence validation concern, not a conflict-routing concern, and is tracked separately."* That "tracked separately" became this project's KI-031.

SPEC §4 itself draws the relevant line: `value_type` is part of the core type table (and SPEC §5.3's Assertion field table lists `value_type` as always required "when literal"), while `required`/`unique`/`constraints` are explicitly marked *"validator-backed, §13"* — i.e. the SPEC already assigns `required` to the plugin/validator layer, not core. This is the strongest argument for treating the two differently, and it is normative, not just a performance preference.

Separately, a `Validator` plugin protocol exists (`plugins/ports.py`) with a shipped reference implementation, `RequiredFieldsValidator` (`plugins/reference/required_fields_validator.py`, KI-010), that performs a presence/absence check: given an assertion, it resolves the subject's concept, re-queries all of the subject's active predicates, and reports which of a *separately configured* required-predicate set are missing. **Two things worth being precise about, corrected in this revision of the ADR after review:**

1. `RequiredFieldsValidator` does **not** read `PropertyDef.required`/`RelationDef.required`. Its `required_predicates` mapping is independently constructor-injected (defaulting to a small worked example, `{"Person": ("name",)}`), unrelated to whatever a schema's `required` field says. A schema author setting `PropertyDef(required=True)` today has no effect on this plugin at all. This is tracked as its own gap, KI-041.
2. No code path in `src/ontolith/` ever calls `Validator.validate()`. `PluginRegistry` (`plugins/registry.py`) registers plugins, resolves capabilities, and builds sandboxed views — it never invokes a registered validator, and neither does any write path in `ontology.py`. `RequiredFieldsValidator` implements the `Validator` protocol; nothing runs it. This is tracked as its own gap, KI-042.

Surfaced during a whole-project milestone audit (2026-07-29, KI-031): the project needed to actually decide, not leave silent, whether `required` gets a core-layer enforcement path — silence here meant an audit could keep re-surfacing the same "is this intentional?" question indefinitely. The decision below was reviewed and its original justification corrected before merge (see Context points 1–2 above): `required` is deferred because SPEC §4 already assigns it to the validator layer and because a per-write core check is structurally the wrong shape for it (see Rationale) — **not** because an existing plugin was already handling it, which was inaccurate.

## Decision

**`value_type`**: enforced in core, at the same call sites and in the same style as the existing unknown-predicate check. `_require_known_predicate` gains an optional `value_type` parameter; when given (only the literal write paths — `assert_literal`, `propose` — pass it; `assert_ref`/`propose_ref` have no `value_type` to check), it looks up `SchemaIR.value_type_of(predicate)` and raises `ValidationError` on a mismatch, mirroring the existing "unknown predicate" `ValidationError`. `SchemaIR.value_type_of()` returns `None` (no check performed) when the predicate resolves to a `RelationDef` rather than a `PropertyDef` — relations don't declare a `value_type`, and a caller writing a literal against a relation-declared predicate is a *predicate-kind* mismatch, a distinct, already-tracked gap (KI-040), not this one.

**`required`**: **stays out of core.** Core does **not** gain a required-field presence check. This is deliberately left as declared-but-unenforced schema metadata for now (surfaced read-only via `GET /schema`/`ontolith.schema`/codegen), consistent with SPEC §4's own "validator-backed" framing. Making it enforced — either by teaching `RequiredFieldsValidator` to derive its rule set from `PropertyDef.required`/`RelationDef.required` (KI-041) and/or by giving the `Validator` protocol an actual invocation point in the write path (KI-042) — is intentionally left as separately tracked follow-up work, not resolved by this ADR.

## Rationale

**Why `value_type` belongs in core:**
- It's a per-assertion, structural check — exactly the same shape as the unknown-predicate check `_require_known_predicate` already performs, at the same call sites, reusing the `SchemaIR` the unknown-predicate check has already loaded (the check itself is an O(1) dict lookup; only *that* increment is free — resolving the schema at all is not, see the p95 discussion below). There's no meaningful cost or design decision left to make; declining to check it would just leave a known type-safety hole for free.
- A type mismatch is a correctness bug, not a business policy question deployments might reasonably disagree on. `Ontology._entity_text` (the reindexing path) already trusts `Assertion.value_type == "Text"` to decide what to concatenate into embeddings; any typed consumer downstream inherits the same trust. Letting a *newly submitted* assertion's `value_type` disagree with the schema's declaration at submission time is a bug with no upside.
- SPEC §4/§5.3 place `value_type` in the core type system, not among the validator-backed constraints (`required`, `unique`, `constraints`) — the SPEC itself draws this line, this ADR just implements it.

**Why `required` stays out of core (three independent arguments, not one):**
- **Structural: a required-field check cannot be a per-assertion write gate at all.** `create_entity` produces an entity with zero assertions. By construction that entity fails every required declaration until its last required assertion lands — there is no single write during which "check required fields now" is both correct and non-disruptive. A core-layer gate would have to either reject the first (and every intermediate) legitimate write on a multi-property entity being built up assertion-by-assertion, or be a no-op until some other trigger decides the entity is "done" — which is exactly the batch/end-of-workflow shape `RequiredFieldsValidator.validate()` already has (called per-assertion, but reporting on the *subject's full current state*, not gating the one write in progress).
- **Normative: SPEC §4 already assigns this to the validator layer** ("required, unique, and constraints (validator-backed, §13)"), distinct from the core type table `value_type` belongs to. Deferring `required` to a `Validator` isn't a performance workaround; it's implementing what the SPEC already specifies.
- **Cost, as a secondary consideration**: the check requires re-querying the full set of the subject's active assertions (`RequiredFieldsValidator.validate()`'s shape), an unbounded-by-entity-size query the `value_type` check never pays. The write path already resolves the schema 2–3 times per write (temporality, cardinality, and now value_type all read the same cached `get_schema()` call), so schema resolution itself isn't the concern — the concern is specifically the *additional* per-write, per-entity assertion re-query a required-fields gate would add, which is a different and larger cost than an in-memory dict lookup against an already-loaded schema.
- It's also a genuinely deployment-specific policy question: which properties are "required" for a concept to be considered complete varies per organization, per use case, and often per migration stage (a `Person` created via bulk import may legitimately lack a `name` for a few minutes before enrichment finishes). A single, unconfigurable core gate would make that judgment call for every deployment.

## Consequences

**Positive:**
- Closes KI-031's `value_type` half: a mistyped literal assertion now fails loudly (`ValidationError`) at submission time instead of silently corrupting the schema's own declared contract.
- Replaces ambiguity ("is `required` core-enforced or not, and does something already handle it?") with an accurate, recorded answer: no, and not yet — tracked forward as two distinct, separately actionable gaps (KI-041: `RequiredFieldsValidator` doesn't read the schema's `required` field; KI-042: no code path invokes any `Validator` at all).
- For any predicate declared as a *property* (`value_type` is a required field on `PropertyDef` — a property can never omit it), the check is always active; the only cases where it doesn't fire are a relation-declared predicate (KI-040's concern, not this ADR's) or a namespace with no schema registered at all.

**Negative / follow-ups:**
- `required` remains schema metadata with no enforcement anywhere in this codebase today — not core, and (per KI-041/KI-042) not really the reference plugin either, despite the plugin's docstring suggesting otherwise. A deployment that wants required-field enforcement must currently write and wire its own `Validator`-calling mechanism from scratch; there is no ready-made, schema-driven answer yet.
- **Enforcement is submission-time only, not schema-lifetime.** `_require_known_predicate`'s value_type check runs when `assert_literal`/`propose` is first called; it is deliberately *not* re-run when a proposal is later replayed (`accept_proposal`/`resubmit` — see `ontology.py`'s existing documented precedent that `_require_known_predicate` is not re-run at replay, matching how the unknown-predicate check already behaves). Two concrete gaps this leaves: (a) a proposal submitted against a schema-less namespace, followed by `apply_schema` before the proposal is accepted, replays unvalidated; (b) a proposal submitted while a predicate is declared `Text`, followed by a schema change to `Integer` before acceptance, commits the stale type. Both are accepted as consistent with existing replay-trust precedent, not new risk introduced by this ADR — but this ADR closes *mismatch at submission*, not *drift across schema evolution*, and should not be read as a stronger guarantee than that.
- The check is case-sensitive exact-string equality against the schema's declared `Literal` value (e.g. `value_type="text"` now raises against a schema declaring `"Text"`) — a second, minor behavior change bundled with the main one. A `value_type` value outside SPEC §4's closed set (e.g. `"Foo"`) is still accepted without error whenever no schema is registered for the namespace, same as before.
- The predicate-kind mismatch this ADR explicitly declines to close (asserting a literal against a schema-declared relation, or vice versa) was tracked as KI-040 and has since been resolved (`SchemaIR.kind_of()` + `_require_known_predicate`'s `expected_kind` parameter) — this bullet is kept for historical context (this ADR's own scope boundary never changed), not as an open item.

## Amendment (2026-08-15): KI-041/KI-042 Resolved

Context points 1–2 above, and the Consequences bullet on line 52, describe the state as of this ADR's original writing (2026-08-04) — accurate then, no longer accurate now, kept unedited as the historical record. As of ADR-0029:

- `RequiredFieldsValidator` **now can** read `PropertyDef.required`/`RelationDef.required`, via a new `from_schema(schema: SchemaIR)` classmethod (KI-041) — the hand-maintained `required_predicates` constructor argument still exists and is still the default, but a deployment can derive it from the schema instead.
- `Ontology` **now does** invoke registered `Validator`s (KI-042) — a new `validators` constructor parameter runs them synchronously per-assertion at every commit point, and a separate `completeness_validators` parameter runs whole-entity-completeness validators like `RequiredFieldsValidator` specifically at `accept_proposal` time. See ADR-0029 for the full design and why the two are split.

This ADR's own Decision (`required` stays out of *core*) is unchanged and still the accurate answer to the question this ADR actually decided — ADR-0029 doesn't move `required` enforcement into core, it makes the validator-layer path SPEC §4 already pointed at actually reachable.

## Alternatives Considered

**Enforce `required` in core too, alongside `value_type`:** Rejected — see Rationale: it isn't just costlier, it's the wrong *shape* of check for a per-assertion write gate (an entity is necessarily incomplete immediately after `create_entity`), and SPEC §4 already assigns it to the validator layer.

**Enforce `required` in core, but only as an opt-in flag on `Ontology`:** Rejected as unnecessary duplication — an opt-in flag that re-implements what a `Validator` should already provide adds a second configuration surface for the same rule, once the plugin/wiring gaps (KI-041, KI-042) are closed.

**Leave `value_type` unenforced too, matching `required`'s deferral:** Rejected — `value_type` and `required` are different in kind (per-assertion structural check the SPEC places in core vs. cross-assertion, validator-backed policy check), not degree; treating them identically was the confusion this ADR resolves, not a reason to defer both.

## References

- SPEC §4 (Schema — property/relation declarations; `required`/`unique`/`constraints` are validator-backed, §13)
- SPEC §13.2 (Validator plugin protocol)
- ADR-0017 (Cardinality-Aware Conflict Routing — where `required`'s deferral was first flagged)
- `src/ontolith/schema/ir.py` (`PropertyDef.value_type`, `PropertyDef.required`, `SchemaIR.value_type_of`)
- `src/ontolith/ontology.py` (`_require_known_predicate`)
- `src/ontolith/plugins/reference/required_fields_validator.py` (`RequiredFieldsValidator`, KI-010 — does not read `PropertyDef.required`, KI-041)
- `src/ontolith/plugins/registry.py` (no invocation point for `Validator.validate()`, KI-042)
- `docs/known-issues.md` (KI-031, KI-040, KI-041, KI-042)
