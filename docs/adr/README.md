# Architecture Decision Records (ADRs)

This directory contains Architecture Decision Records for Ontolith, documenting significant design decisions and their rationale.

## Format

We use the [MADR](https://adr.github.io/madr/) (Markdown Any Decision Records) format.

## Index

### Core Decisions (from PRD §16)

- [ADR-0001](ADR-0001-storage-default.md) — **Storage Default** (SQLite + sqlite-vec)
- [ADR-0002](ADR-0002-schema-definition.md) — **Schema Definition** (Class DSL + LinkML YAML via IR)
- [ADR-0003](ADR-0003-agent-identity.md) — **Agent Identity** (Service principal + accountable owner + per-assertion model)
- [ADR-0004](ADR-0004-confidence-semantics.md) — **Confidence Semantics** (Single scalar, no auto-combine in v1)
- [ADR-0005](ADR-0005-conflict-model.md) — **Conflict Model** (Routing by temporality: supersession vs contradiction)
- [ADR-0006](ADR-0006-licensing-and-business.md) — **Licensing & Business** (Open-core: Apache-2.0 + proprietary managed plane)
- [ADR-0007](ADR-0007-interop-priority.md) — **Interop Priority** (LinkML → RDF/OWL → agent-memory bridges)
- [ADR-0008](ADR-0008-mcp-surface.md) — **MCP Surface** (Read + propose, no direct write)

## Decision Process

1. **Propose** — Create ADR in draft status with context and options
2. **Discuss** — Review in team/community (via PR or RFC)
3. **Decide** — Update status to "Accepted" or "Rejected"
4. **Implement** — Reference ADR in implementation PRs
5. **Supersede** — If decision changes, create new ADR and mark old one superseded

## Status Values

- **Proposed** — Under discussion
- **Accepted** — Decision made and active
- **Rejected** — Decision considered but not adopted
- **Deprecated** — Previously accepted, now superseded
- **Superseded by ADR-XXXX** — Replaced by a newer decision

## Adding New ADRs

When making significant architectural decisions:

1. Copy the MADR template
2. Number sequentially (ADR-0009, ADR-0010, ...)
3. Use descriptive filename: `ADR-XXXX-short-title.md`
4. Fill in: Context, Decision, Rationale, Consequences, Alternatives
5. Reference related SPEC sections, PRD requirements, and other ADRs
6. Add to this index
7. Create PR for review

## Cross-References

- **PRD** — Product Requirements Document (`../Ontolith_PRD.md`)
- **SPEC** — Technical Specification (`../Ontolith_SPEC.md`)
- **Implementation Plan** — Engineering roadmap (`../Ontolith_Implementation_Plan.md`)
