# ADR-0004: Confidence Semantics

**Status:** Accepted

**Date:** 2026-06-20

**Deciders:** Ontolith Core Team

## Context

Assertions carry confidence scores to represent uncertainty. We need to decide:
- What confidence means (author's belief vs system-derived score)
- Whether/how to combine confidence from multiple sources
- How to evolve the semantics without breaking changes

## Decision

**Single scalar `0.0–1.0` (principal's stated belief) + open metadata blob; no auto-combine in v1.**

**Confidence Field:**
- Float between 0.0 (no confidence) and 1.0 (complete confidence)
- Represents the **asserting principal's stated belief** that the assertion is true
- Distinct from any system-derived corroboration score
- NULL allowed (means "unspecified," policy may treat as below threshold)

**Metadata Blob:**
- Open JSON field alongside confidence
- Reserved for future evolution (per-source scores, aggregation metadata)
- Must be preserved round-trip (forward compatibility)

**No Auto-Combine (v1 Limitation):**
- Corroborating assertions are **surfaced, not merged**
- No automatic combination of confidence scores
- Multiple assertions about the same fact stay separate
- Caller decides how to interpret (query can rank by confidence × trust)

## Rationale

**Why single scalar:**
- Simple to reason about (author's subjective probability)
- Maps to LLM calibration research (temperature, etc.)
- Queryable (rank by confidence)
- Avoids premature complexity

**Why "stated belief" not "system score":**
- Author is accountable for their confidence
- Different from post-hoc aggregation or corroboration
- Preserves provenance (this source said 0.8, that source said 0.6)

**Why no auto-combine:**
- Combining strategies are domain-specific (mean? max? weighted?)
- Corroboration semantics unclear (does agreement increase confidence?)
- v1 surfaces corroborating assertions; caller decides
- Avoids wrong defaults that are hard to change

**Why metadata blob:**
- Future-proofs for richer models (per-source, aggregated, calibrated)
- No schema migration needed to add fields
- Keeps v1 simple, v2+ flexible

## Consequences

**Positive:**
- ✅ Clear semantics (author's belief)
- ✅ Simple to implement and reason about
- ✅ Future-proof (metadata blob)
- ✅ No premature aggregation logic

**Negative:**
- ⚠️ Callers must handle corroboration themselves
- ⚠️ No built-in "overall confidence" for multi-source facts

**Mitigations:**
- Query API surfaces corroborating assertions (ranked by confidence × trust)
- Documentation explains semantics clearly
- v2+ can add aggregation strategies without breaking v1

## Alternatives Considered

**Multi-scalar (per-source, aggregated, calibrated):**
- Rejected for v1: Too complex, unclear semantics

**Auto-combine (mean/max/weighted):**
- Rejected: Wrong default is worse than no default

**No confidence field:**
- Rejected: Uncertainty is critical for AI-authored facts

## References

- PRD §7 (Core concepts: Confidence)
- PRD §16 Decision 4
- SPEC §5.3 (Assertion: confidence field)
- SPEC §7 (Assertion & provenance semantics)
