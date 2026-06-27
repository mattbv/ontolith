# ADR-0007: Interop Priority (LinkML → RDF/OWL → Agent Memory)

**Status:** Accepted

**Date:** 2026-06-20

**Deciders:** Ontolith Core Team

## Context

Ontolith must interoperate with:
- **Ontology standards** (LinkML, RDF, OWL) for semantic web / data integration
- **Agent memory systems** (Mem0, Zep, Cognee, etc.) for AI ecosystem

We need to prioritize bridge development to maximize reach per effort.

## Decision

**LinkML → RDF/OWL → agent-memory bridges.**

**Phase 1 (M2): LinkML**
- Export Ontolith schemas to LinkML
- Import LinkML schemas to Ontolith
- Round-trip fidelity (golden tests)

**Phase 2 (M3): RDF/OWL**
- Export via LinkML → RDF/OWL (piggyback on LinkML's emission)
- Direct Ontolith → RDF/OWL (native bridge)
- OWL reasoning integration (via Reasoner plugin)

**Phase 3 (vNext): Agent Memory**
- Import from Mem0, Zep/Graphiti, Cognee
- Export to common agent memory formats
- Bidirectional sync (as Connector plugins)

## Rationale

**Why LinkML first:**
- YAML schema dialect (ADR-0002) is **already LinkML-aligned**
- Bridge is largely a projection (minimal work, high reach)
- LinkML round-trips to JSON/RDF → partial RDF reach immediately
- Active ecosystem, NIH/standards backing
- Compounds with schema decision

**Why RDF/OWL second:**
- LinkML provides path (piggyback on emission)
- Semantic web standards = wide reach
- OWL reasoning is a requested feature
- Stable, mature ecosystem

**Why agent-memory last:**
- Ecosystem is fluid (tools change fast)
- More about ingestion than standards
- High value but less urgent for v1
- Better as Connector plugins (many → many)

**Reach per effort:**
- LinkML: high reach (via RDF transitively), low effort (already aligned)
- RDF/OWL: very high reach (standards), medium effort
- Agent memory: medium reach (new ecosystem), high effort (many tools)

## Consequences

**Positive:**
- ✅ Fastest path to semantic web interop (LinkML)
- ✅ Leverage schema decision (YAML already aligned)
- ✅ RDF comes nearly free (via LinkML)
- ✅ Agent memory deferred until ecosystem stabilizes

**Negative:**
- ⚠️ Agent memory users wait longer
- ⚠️ Dependency on LinkML's RDF fidelity

**Mitigations:**
- Agent memory as Importer plugins (community can contribute)
- Direct RDF/OWL bridge (M3) if LinkML → RDF insufficient

## Alternatives Considered

**RDF/OWL first:**
- Rejected: More effort, LinkML transitively gives RDF anyway

**Agent memory first:**
- Rejected: Ecosystem too fluid, less standardized

**All in parallel:**
- Rejected: Spreads resources thin, delays core features

## References

- PRD §10 (Plugin types: Importer, Exporter)
- PRD §16 Decision 7
- SPEC §13.3 (Interop sequence)
- ADR-0002 (Schema definition)
