# ADR-0005: Conflict Model (Routing by Temporality)

**Status:** Accepted

**Date:** 2026-06-20

**Deciders:** Ontolith Core Team

## Context

When multiple sources assert different values for the same property, we need to handle conflicts. Two distinct patterns emerge:

1. **The world changed** — Value was X, now it's Y (e.g., job change)
2. **Sources disagree** — Alice says X, Bob says Y (true contradiction)

The system must distinguish these and route them differently.

## Decision

**Both, routed by temporality**: `time_varying` → supersession; `static` → contradiction. Default `static`.

**Temporality Attribute:**
- Per-property/relation declaration in schema
- `static` (default) or `time_varying`
- Determines conflict handling behavior

**Temporal Supersession (time_varying):**
- Close prior validity window (`valid_to = new.valid_from`)
- Mark old assertion as `superseded`
- Link successor chain (`new.supersedes = old.id`)
- **No review triggered** (change over time is expected)
- Multiple values coexist if windows don't overlap

**Contradiction (static):**
- Create/extend `Contradiction` object linking conflicting assertions
- Mark all members as `flagged`
- **Route to review** (unexpected, requires human judgment)
- Rank members by confidence × source trust
- Flagged assertions retained and queryable but excluded from default results

**Resolution:**
- Review selects winner or asserts new value
- Losers marked `retracted`
- Contradiction state = `resolved`
- Resolution event in provenance

## Rationale

**Why route by temporality:**
- "World changed" vs "sources disagree" are fundamentally different
- Temporal supersession is cheap (no review)
- Static contradictions surface disagreements rather than burying them

**Why default static:**
- Safe default: surfaces conflicts rather than silently overwriting
- Forces explicit declaration for time-varying properties
- Prevents accidental data loss

**Why supersession ≠ contradiction:**
- Supersession: "value was X at t1, now Y at t2" (timeline)
- Contradiction: "Alice says X, Bob says Y" (disagreement)
- Different semantics, different handling

**Why keep flagged assertions:**
- Audit trail (what did we almost believe?)
- May become relevant later (resolver was wrong)
- Queryable for debugging

## Consequences

**Positive:**
- ✅ Honest conflict handling (doesn't hide disagreements)
- ✅ Temporal supersession is efficient (no review for expected changes)
- ✅ Static facts protected from silent overwrite
- ✅ Clear mental model (temporality drives routing)

**Negative:**
- ⚠️ Developers must understand temporality
- ⚠️ Static facts create contradictions (requires review)

**Mitigations:**
- Documentation emphasizes default=static rationale
- Schema validation helps (declare temporality explicitly)
- Contradictions are queryable/resolvable

## Alternatives Considered

**Always supersede:**
- Rejected: Loses "sources disagree" signal

**Always contradict:**
- Rejected: Makes temporal data painful (every change → review)

**Auto-resolve by confidence:**
- Rejected: "Highest confidence wins" hides disagreements

**LWW (last-write-wins):**
- Rejected: Silently overwrites, loses minority view

## References

- PRD §7 (Core concepts: Temporality, Contradiction)
- PRD §16 Decision 5
- SPEC §10 (Conflict semantics)
