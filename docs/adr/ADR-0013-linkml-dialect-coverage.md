# ADR-0013: LinkML Dialect Coverage (v1)

**Status:** Accepted

**Date:** 2026-07-05

**Deciders:** Ontolith Core Team

**Related:** ADR-0002 (Schema Definition), ADR-0007 (Interop Priority), SPEC §6.1 (One IR, two front-ends), SPEC Appendix B (open implementation questions)

---

## Context

SPEC §6.1 requires the YAML schema front-end to be "a strict subset/superset documented against LinkML so the LinkML bridge (§13, interop) is largely a projection." SPEC Appendix B explicitly lists "LinkML dialect coverage" as an open implementation question. Full LinkML conformance is a large surface (shared slots with `slot_usage` overrides, class inheritance via `is_a`/mixins, enums, boolean slot combinators, pattern constraints, multi-file schemas with `imports`) — implementing all of it is not the goal; a strict, well-documented subset is.

This ADR also decides the mechanics for two IR concepts LinkML has no native equivalent for: Ontolith's `temporality` (`static`/`time_varying`) and whether to depend on the real `linkml`/`linkml-runtime` PyPI packages.

## Decision

### v1 dialect scope

**In scope:**
- Schema-level `id`/`name` (→ `namespace`), `version` (parsed as a plain integer — a deliberate, documented deviation from LinkML's typical free-text `version` field), `prefixes`/`default_prefix`/`description` (preserved verbatim in `SchemaIR.metadata`, not interpreted).
- `classes.<Name>` → `ConceptDef`, with **only inline `attributes:` per class**.
- `attributes.<name>.range`: a builtin scalar type name (via the fixed mapping table below) → `PropertyDef`; a name matching another class in the same schema → `RelationDef`.
- `multivalued: true/false` → `cardinality: many/single`.
- `required: true/false` → `PropertyDef.required`/`RelationDef.required`.
- Native LinkML `inverse:` → `RelationDef.inverse` (LinkML already has this concept; no extension needed).
- `description:` on both classes and slots.

**Explicitly out of scope for v1 (fail-loud on import, not silently dropped):**
- Shared top-level `slots:` dict / slot reuse across classes, `slot_usage` overrides.
- Class inheritance (`is_a`, `mixins`, `tree_root`).
- `enums`, `permissible_values`.
- Pattern/boolean constraints (`pattern`, `any_of`, `all_of`, `exactly_one_of`, `none_of`).
- `imports` / multi-file schemas.
- `types` (custom scalar type definitions) and `subsets`.

The single biggest scope-reducer is **inline attributes only** — Ontolith-authored schemas (round-tripped from the class DSL) never need shared slots anyway; this mainly affects importing hand-written external LinkML files that use the shared-slot idiom, which now fail loudly with a clear `SchemaError` naming the unsupported construct, rather than silently dropping data.

### Type mapping table

| Ontolith `value_type` | LinkML `range` |
|---|---|
| Text | string |
| Integer | integer |
| Float | float (also accepts `double` on import) |
| Boolean | boolean |
| Date | date |
| DateTime | datetime |
| URI | uriorcurie (also accepts `uri` on import) |
| JSON | string, plus `annotations.ontolith_value_type: JSON` (see below) |

### Temporality via `annotations`

LinkML has no native temporal-behavior concept. Ontolith's `temporality` is encoded as `annotations.ontolith_temporality: time_varying` on the slot — LinkML's `annotations:` block is designed exactly for tool-specific extensions like this. `static` (the default) is omitted entirely rather than written explicitly, keeping generated YAML minimal.

### Disambiguating JSON from Text via `annotations`

`JSON` has no clean LinkML scalar equivalent and was initially mapped to `range: string` — the same range as `Text`. That collapses the two on import (`from_yaml(to_yaml(json_property))` silently produced a `Text` property), breaking round-trip fidelity, the exact failure mode this ADR's fail-loud policy exists to prevent. Fixed the same way as temporality: a `JSON`-typed property additionally carries `annotations.ontolith_value_type: JSON`, and `from_yaml` checks this annotation before falling back to the range-based type-mapping table.

### Fail-loud, not best-effort

`from_yaml` raises `SchemaError` naming the specific unsupported construct (schema-level, class-level, or slot-level) the moment one is encountered, rather than silently dropping it. A silently-lossy importer would pass tests today and corrupt data later — this protects the round-trip-fidelity guarantee the golden tests exist to prove. `to_yaml` only ever emits constructs `from_yaml` can parse back, so exporter output is always re-importable by construction.

### No dependency on the real `linkml`/`linkml-runtime` packages

Implemented directly against `PyYAML` (new `interop` optional-dependency extra, folded into `all`). The real LinkML tooling packages are heavy (pull in `rdflib`, `prefixcommons`, JSON-LD context machinery, etc.) for a dialect this deliberately scoped-down, and full LinkML conformance is not the v1 goal per ADR-0002/SPEC §6.1's "strict subset/superset" framing.

## Consequences

**Positive:**
- Resolves SPEC Appendix B's open "LinkML dialect coverage" question with a concrete, enumerable answer.
- `to_yaml`/`from_yaml` round-trip fidelity is provable by construction (fail-loud on anything not emittable).
- No heavy transitive dependency footprint from the real LinkML package family.

**Negative:**
- Hand-written external LinkML schemas using shared slots, inheritance, or enums will not import — this is a real, user-facing limitation, not a bug. Widening dialect coverage is future work if demand emerges (tracked as a known issue alongside KI-010's closure).
- `version` as a plain integer is a minor deviation from typical LinkML usage (where `version` is often a free-text string like `"1.0.3"`) — acceptable since LinkML's `version` field is untyped free text, so an integer is still valid LinkML, just non-idiomatic.

## Alternatives Considered

**Full LinkML conformance via the real `linkml`/`linkml-runtime` packages:** Rejected for v1 — large dependency footprint and surface area disproportionate to what Ontolith's schema model actually needs (Ontolith has no native concept of enums, class inheritance, or boolean slot combinators to map these onto anyway).

**Best-effort/lossy import (silently drop unsupported constructs):** Rejected — would pass the golden round-trip tests today while quietly corrupting imported schemas that use any excluded construct, discovered only much later by an end user.

**Shared top-level `slots:` support in v1:** Rejected — meaningfully more parsing/resolution logic (slot reuse, `slot_usage` override merging) for a construct Ontolith-authored schemas never produce.

## References

- SPEC §6.1: One IR, two front-ends
- SPEC Appendix B: Open implementation questions
- ADR-0002: Schema Definition
- ADR-0007: Interop Priority (LinkML → RDF/OWL → Agent Memory)
