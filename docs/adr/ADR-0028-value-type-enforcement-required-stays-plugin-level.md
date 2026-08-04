# ADR-0028: `value_type` Enforced at Core Write Time; `required` Stays Plugin-Level

**Status**: Accepted
**Date**: 2026-08-04
**Deciders**: Ontolith Core Team
**Related**: SPEC §4 (Schema), ADR-0017 (Cardinality-Aware Conflict Routing), KI-010 (reference plugins), KI-031, KI-040

---

## Context

`PropertyDef` (`src/ontolith/schema/ir.py`) declares two constraints beyond temporality/cardinality: `value_type` (e.g. `Integer`, `Text`, `Date`) and `required`. Neither was consulted anywhere on the write path. `Ontology._require_known_predicate` rejected an *unknown* predicate but not a *mistyped* one: `assert_literal(..., predicate="Person.age", value_type="Text", ...)` against a predicate declared `value_type: Integer` was silently accepted. `required` was never checked at all — an entity missing a schema-declared-required property raised nothing anywhere in `core`.

ADR-0017 already flagged this gap while scoping cardinality enforcement, explicitly deferring it: *"`required`... is explicitly out of scope for this ADR — it's a presence/absence validation concern, not a conflict-routing concern, and is tracked separately."* That "tracked separately" became this project's KI-031.

Separately, a `Validator` plugin protocol already exists (`plugins/ports.py`) with a shipped reference implementation, `RequiredFieldsValidator` (`plugins/reference/required_fields_validator.py`, KI-010), that does exactly the presence/absence check ADR-0017 described: given an assertion, it resolves the subject's concept, re-queries all of the subject's active predicates, and reports which of a configured required set are missing. It ships with a small worked default (`{"Person": ("name",)}`) and is designed to be reconfigured per deployment.

Surfaced during a whole-project milestone audit (2026-07-29, KI-031): the project needed to actually decide, not leave silent, whether `required` gets a second, core-layer enforcement path alongside the existing plugin one — silence here meant an audit could keep re-surfacing the same "is this intentional?" question indefinitely.

## Decision

**`value_type`**: enforced in core, at the same call sites and in the same style as the existing unknown-predicate check. `_require_known_predicate` gains an optional `value_type` parameter; when given (only the literal write paths — `assert_literal`, `propose` — pass it; `assert_ref`/`propose_ref` have no `value_type` to check), it looks up `SchemaIR.value_type_of(predicate)` and raises `ValidationError` on a mismatch, mirroring the existing "unknown predicate" `ValidationError`. `SchemaIR.value_type_of()` returns `None` (no check performed) when the predicate resolves to a `RelationDef` rather than a `PropertyDef` — relations don't declare a `value_type`, and a caller writing a literal against a relation-declared predicate is a *predicate-kind* mismatch, a distinct, already-tracked gap (KI-040), not this one.

**`required`**: **stays plugin-level.** Core does **not** gain a second, parallel required-field check. `RequiredFieldsValidator` remains the answer for "does this entity have everything it needs" — this ADR records that as the deliberate, permanent division of responsibility, not an oversight to keep revisiting.

## Rationale

**Why `value_type` belongs in core:**
- It's a per-assertion, structural check — exactly the same shape as the unknown-predicate check `_require_known_predicate` already performs, at the same call sites, using the schema lookup that's already happening on every write. There's no meaningful cost or design decision left to make; declining to check it would just leave a known type-safety hole for free.
- A type mismatch is a correctness bug, not a business policy question deployments might reasonably disagree on. `Ontology._entity_text` (the reindexing path) already trusts `Assertion.value_type == "Text"` to decide what to concatenate into embeddings; any typed consumer downstream inherits the same trust. Letting `value_type` drift from the schema's declaration silently corrupts that trust with no recourse.
- "Validate at edges, trust within" (project convention) doesn't apply here — `value_type` isn't a boundary-input-shape concern (Pydantic already handles that at the SDK/REST/MCP boundary), it's a domain invariant: an assertion's declared type should match what the schema says that predicate holds.

**Why `required` stays out of core:**
- It is not a per-assertion check — by construction, you cannot know whether an entity satisfies its required predicates by looking at the one assertion currently being written; it requires re-querying the full set of the subject's active assertions (exactly what `RequiredFieldsValidator.validate()` does). Running that on every single `assert_literal`/`assert_ref`/`propose`/`propose_ref` call would add an unbounded-by-entity-size query to the hot write path, directly threatening the SPEC's `propose` + policy eval + commit p95 < 50ms budget — a cost `value_type`'s O(1) schema-dict lookup never pays.
- It's a genuinely deployment-specific policy question, not a universal structural invariant: which properties are "required" for a concept to be considered complete varies per organization, per use case, and often per migration stage (a `Person` created via bulk import may legitimately lack a `name` for a few minutes before enrichment finishes). Hard-coding a single, unconfigurable required-field gate into core would make that judgment call for every deployment; the plugin's constructor-injected `required_predicates` mapping is the right place for a decision that varies this much.
- `RequiredFieldsValidator` already exists, is already wired into the `Validator` protocol, and already does this correctly. Building a second, core-layer version of the same check would be duplicate machinery enforcing the same rule two different ways with two different configuration surfaces — worse than either alone.

## Consequences

**Positive:**
- Closes KI-031's `value_type` half completely: a mistyped literal assertion now fails loudly (`ValidationError`) instead of silently corrupting the schema's own declared contract.
- Closes the "is `required` core-enforced or not?" ambiguity ADR-0017 left open — the answer is now recorded, not silent.
- No behavior change for any predicate that omits `value_type` from schema, or for any namespace with no schema registered at all (both existing `_require_known_predicate` no-op paths are preserved).

**Negative / follow-ups:**
- A deployment that wants required-field enforcement must explicitly configure/enable `RequiredFieldsValidator` (or an equivalent custom `Validator`); core provides no default guard rail here. This is accepted as correct, not a gap — see Rationale.
- The predicate-kind mismatch this ADR explicitly declines to close (asserting a literal against a schema-declared relation, or vice versa) remains open, tracked as KI-040.

## Alternatives Considered

**Enforce `required` in core too, alongside `value_type`:** Rejected — the performance cost (a full re-query of the subject's assertions on every write) and the deployment-specific nature of "what counts as required" both argue against a single, unconfigurable core gate. See Rationale.

**Enforce `required` in core, but only as an opt-in flag on `Ontology`:** Rejected as unnecessary duplication — an opt-in flag that re-implements what `RequiredFieldsValidator` already does adds a second configuration surface for the same rule, with no behavior a plugin couldn't already provide.

**Leave `value_type` unenforced too, matching `required`'s deferral:** Rejected — `value_type` and `required` are different in kind (per-assertion structural check vs. cross-assertion policy check), not degree; treating them identically was the confusion this ADR resolves, not a reason to defer both.

## References

- SPEC §4 (Schema — property/relation declarations)
- ADR-0017 (Cardinality-Aware Conflict Routing — where `required`'s deferral was first flagged)
- `src/ontolith/schema/ir.py` (`PropertyDef.value_type`, `PropertyDef.required`, `SchemaIR.value_type_of`)
- `src/ontolith/ontology.py` (`_require_known_predicate`)
- `src/ontolith/plugins/reference/required_fields_validator.py` (`RequiredFieldsValidator`, KI-010)
- `docs/known-issues.md` (KI-031, KI-040)
