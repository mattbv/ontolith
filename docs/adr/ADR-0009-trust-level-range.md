# ADR-0009: Trust Level Range (0-10)

**Status**: Accepted  
**Date**: 2026-06-25  
**Deciders**: Ontolith Core Team  
**Related**: SPEC §8 (Principal model), ADR-0003 (Agent Identity)

---

## Context

The Principal model includes a `trust_level` field (integer) used in policy evaluation and query filtering (e.g., `.trust_at_least(2)`). The SPEC §8.3 references trust levels but does not specify a valid range, leaving it as an unbounded integer.

**Problem**: An unbounded integer has unclear semantics:
- What does trust level 1000 mean vs. 100?
- How do policies interpret values?
- No validation prevents nonsensical values (negative, extremely large)

**Usage in system**:
- Policy decisions (TrustLevel strategy)
- Query filtering (`.trust_at_least(n)`)
- Ranking in hybrid retrieval (`confidence × trust`)
- Per-namespace overrides (principal_trust table)

---

## Decision

We constrain `trust_level` to the range **0-10** (inclusive).

**Semantics**:
- **0**: Untrusted / default for new principals
- **1-4**: Low trust (may require review for most operations)
- **5-7**: Moderate trust (common for established users)
- **8-10**: High trust (can bypass some review workflows)

**Enforcement**:
- Database CHECK constraint: `trust_level >= 0 AND trust_level <= 10`
- Pydantic validation: `Field(ge=0, le=10)`
- Default value: `0` (untrusted until explicitly granted)

---

## Rationale

### Why constrain at all?
1. **Policy safety**: Prevents bugs from extreme values in trust-based decisions
2. **Semantic clarity**: Bounded scale has clear meaning (similar to 0-1 confidence)
3. **Interoperability**: Standard range enables cross-system trust mappings

### Why 0-10 specifically?
1. **Simplicity**: Small enough to reason about ("this user is a 7")
2. **Granularity**: 11 levels provide sufficient differentiation without over-precision
3. **Familiarity**: Common scale in rating systems (matches human intuition)
4. **Not percentage**: Avoids false precision of 0-100 when trust is inherently subjective

### Alternatives considered:

**0-100 (rejected)**
- Pro: Matches confidence range
- Con: False precision—trust is not measurable to 1% accuracy
- Con: Harder to reason about ("is this user a 73 or 74?")

**Unbounded (rejected)**
- Pro: Maximum flexibility
- Con: No semantic meaning
- Con: Policy bugs from unexpected values (e.g., overflow, negative)

**Boolean (rejected)**
- Pro: Simplest possible
- Con: Too coarse—can't distinguish "new user" from "established user" from "admin"

---

## Consequences

### Positive
- Clear semantics for trust levels
- Validation prevents nonsensical values
- Policies can rely on bounded range (e.g., "trust >= 8 for direct write")
- Per-namespace overrides stay within comprehensible bounds

### Negative
- Migration concern: If SPEC later mandates different range, requires schema migration
  - **Mitigation**: Range is conservative (can expand 0-10 → 0-100 without breaking existing data)
- Subjectivity: "What is a 7?" depends on organizational policy
  - **Mitigation**: Documentation provides guidelines; organizations customize policies

### Neutral
- Default of 0 is restrictive (untrusted)
  - **Rationale**: Fail-safe—principals must explicitly earn trust

---

## Implementation

### Schema (SQLite)
```sql
CREATE TABLE principal (
  ...
  trust_level INTEGER NOT NULL DEFAULT 0 CHECK(trust_level BETWEEN 0 AND 10),
  ...
);
```

### Model (Pydantic)
```python
class Principal(BaseModel):
    trust_level: int = Field(default=0, ge=0, le=10)
```

### Tests
- Boundary tests: 0, 10 accepted
- Out-of-bounds: -1, 11 rejected
- Default: 0 when omitted

---

## Notes

- This decision is implementation-specific (not SPEC-mandated)
- If SPEC later defines a range, we'll align via migration
- Semantics guidelines in documentation, not enforced by code
- Per-namespace overrides (principal_trust table) inherit same 0-10 range
