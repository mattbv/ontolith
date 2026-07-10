# ADR-0012: Class DSL Compiler Design

**Status:** Accepted

**Date:** 2026-07-05

**Deciders:** Ontolith Core Team

**Related:** ADR-0002 (Schema Definition), SPEC §6.1 (One IR, two front-ends), SPEC §6.2 (Class DSL)

---

## Context

SPEC §6.1 requires bidirectional codegen between two front-ends and the canonical IR: `classes → IR → YAML` and `YAML → IR → class stubs`. Only the IR side (`SchemaIR`/`ConceptDef`/`PropertyDef`/`RelationDef` in `src/ontolith/schema/ir.py`) existed prior to this ADR — the class DSL itself (SPEC §6.2's `class Person(Concept): name: Text`) was never built, despite being documented as the primary developer-facing front-end and listed as M1 scope in the Implementation Plan.

Several design questions had to be settled to compile Python class bodies into `SchemaIR`:

1. How to distinguish scalar value types (`Text` vs `URI`, both of which are `str` at runtime) during introspection.
2. How to express a relation's target concept (`Ref["Organization"]`) without requiring the target class to already exist — mutually-referencing concepts (`Person.employer → Organization`, `Organization.employees → Person`) can't both be defined first.
3. Whether concepts support inheritance.
4. Whether compilation is triggered by class definition alone (an implicit global registry of every `Concept` subclass ever imported) or by an explicit call.
5. Whether properties, not just relations, need a field-spec function to express `temporality`/`cardinality`/`required` beyond what a plain annotation can carry.

## Decision

### Scalar types as `Annotated` markers, not bare aliases

`Text`, `Integer`, `Float`, `Boolean`, `Date`, `DateTime`, `URI`, `JSON` are each `Annotated[<python_type>, _ScalarMarker("Text")]` rather than `Text = str`. A bare alias makes `Text` and `URI` both resolve to runtime type `str`, indistinguishable during introspection. `Annotated` metadata, recovered via `typing.get_type_hints(cls, include_extras=True)`, lets the compiler read back the exact `value_type` literal.

### `Ref["TargetConcept"]` captures a string, not a real generic

`Ref` is implemented via `__class_getitem__`, returning `Annotated[str, _RefMarker("Organization")]` — the target concept name is captured as a plain string, never resolved as a real forward reference. This is what makes mutually-referencing concepts compile without needing either class to exist yet when the other's body is evaluated; validation that the target concept actually exists happens later, for free, via `SchemaIR`'s existing `validate_relation_targets` model validator.

### No concept inheritance in v1

`ConceptMeta` only walks a concept's own class body — it does not merge fields from base classes. This matches the current flat (non-inheriting) `ConceptDef` shape in the IR. Revisit if/when the IR itself gains an inheritance concept.

### Explicit `compile_schema()`, no implicit global registry

`compile_schema(namespace, version, *concepts, metadata=None) -> SchemaIR` takes the concepts to include as explicit arguments. There is no hidden module-level list of "every `Concept` subclass ever defined," which would create import-order-dependent behavior and hidden mutable global state — inconsistent with the project's Clock/IdProvider injection philosophy (the Implementation Plan's determinism invariant: no hidden non-determinism in domain logic).

### `Property(...)` added alongside `Relation(...)`

SPEC §6.2's example only shows `Relation(...)` used as a field spec; properties are shown only as plain annotations (`born: Date | None = None`), which can't express `temporality="time_varying"` or non-default `cardinality` on a *property*. `Property(*, cardinality="single", required=False, temporality="static", description=None)` is added for symmetry — a small, natural extension consistent with "Pydantic-style declarations," not a deviation from the IR (which already supports `time_varying` properties).

### Decompiler output is deterministic

`generate_class_stubs(schema: SchemaIR) -> str` sorts concepts by name and preserves the IR's own dict insertion order for fields, so repeated calls against the same `SchemaIR` produce byte-identical output — required for the `classes → IR → YAML → IR → classes` golden idempotence test (Implementation Plan §6).

## Consequences

**Positive:**
- SPEC §6.2's literal example (`from ontolith import Concept, Relation, Text, Date, Ref`) now works verbatim.
- `classes → IR` and `IR → classes` (via generated stubs) are both implemented, completing the "two front-ends" half of SPEC §6.1 that doesn't depend on the YAML loader (ADR-0013).
- No new global mutable state; compilation is fully explicit and testable in isolation.

**Negative:**
- No concept inheritance — schemas needing shared base fields across concepts must duplicate them until inheritance is designed.
- `Property(...)` is not literally in SPEC §6.2's example; documented here as an intentional, backwards-compatible addition rather than a deviation from the IR itself.

## Alternatives Considered

**Bare type aliases (`Text = str`):** Rejected — indistinguishable from other `str`-backed types during introspection, breaking round-trip fidelity.

**Real `typing.Generic[T]` for `Ref`:** Rejected — would require the target class to be resolvable (importable) at annotation-evaluation time, breaking mutual references between concepts.

**Implicit global `Concept` registry (e.g., `__init_subclass__` appending to a module-level list):** Rejected — import-order-dependent, hidden state, and inconsistent with the project's explicit-injection determinism philosophy.

## References

- SPEC §6.1: One IR, two front-ends
- SPEC §6.2: Class DSL (Primary DX)
- ADR-0002: Schema Definition
