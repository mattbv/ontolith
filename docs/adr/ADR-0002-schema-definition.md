# ADR-0002: Schema Definition (Class DSL + LinkML YAML via IR)

**Status:** Accepted

**Date:** 2026-06-20

**Deciders:** Ontolith Core Team

## Context

We need developers to define ontologies (concepts, properties, relations) in a way that is:
- Pythonic and ergonomic for code-first workflows
- Portable and interchangeable for data-first workflows
- Round-trippable between representations
- A foundation for LinkML interop

## Decision

**Both, via one IR**: class-based DSL ships first (M1), LinkML-aligned YAML ships 0.2, bidirectional codegen between them.

**Internal Representation (IR):**
- Single source of truth: JSON document per namespace
- Stored as `schema_version` rows (immutable, enables time-travel)

**Two front-ends compile to IR:**
1. **Class-based Python DSL** (primary DX, ships M1):
   ```python
   class Person(Concept):
       name: Text
       born: Date | None = None
       employer: Ref["Organization"] = Relation(temporality="time_varying")
   ```

2. **LinkML-aligned YAML** (ships 0.2):
   - Subset/superset of LinkML that round-trips cleanly
   - Enables data-first workflows
   - Foundation for LinkML bridge

**Codegen:**
- `classes → IR → YAML` (export)
- `YAML → IR → class stubs` (import)

## Rationale

**Why class DSL:**
- Best DX for Python developers
- Type hints provide IDE support
- Familiar Pydantic-style syntax
- Natural expression of the model

**Why LinkML YAML:**
- Portable, interchange-friendly format
- Required for LinkML bridge (M2 priority)
- Enables data-first workflows (schema from YAML, not code)
- Industry-standard ontology format

**Why both via IR:**
- Avoids dual sources of truth
- Schema evolution happens once (on IR)
- Codegen keeps representations in sync
- Migration logic operates on IR

**Why IR as JSON:**
- Language-neutral
- Versionable (store as `schema_version` rows)
- Inspectable and toolable

## Consequences

**Positive:**
- ✅ Best DX for devs (class DSL)
- ✅ Portable for standards (YAML)
- ✅ Round-trip fidelity (codegen)
- ✅ LinkML bridge is projection (YAML already aligned)

**Negative:**
- ⚠️ Two syntaxes to maintain (codegen must stay in sync)
- ⚠️ LinkML coverage requires careful dialect design

**Mitigations:**
- Ship class DSL first (M1), validate DX before YAML
- Golden tests for round-trip fidelity
- LinkML dialect documented against LinkML spec

## Alternatives Considered

**Class DSL only:**
- Rejected: Limits interop, no portable format

**YAML only:**
- Rejected: Poor DX for Python developers, verbose

**Existing schema lib (Pydantic, dataclasses, attrs):**
- Rejected: Don't model temporality, relations, or axioms natively

## References

- PRD §8 P1 (Ontology modeling core)
- PRD §16 Decision 2
- SPEC §6 (Schema definition system)
- Implementation Plan: M1 ships class DSL, M2 ships YAML
