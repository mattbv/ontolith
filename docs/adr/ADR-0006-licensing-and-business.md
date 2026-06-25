# ADR-0006: Licensing & Business Model (Open-Core)

**Status:** Accepted

**Date:** 2026-06-20

**Deciders:** Ontolith Core Team

## Context

Ontolith is designed as a framework/SDK that others will build on. We need a licensing strategy that:
- Maximizes adoption (frictionless for developers)
- Enables a sustainable business (managed offering)
- Protects against cloud provider competitors
- Preserves contributor rights for dual licensing

## Decision

**Open-core**: Apache-2.0 on OSS core, proprietary managed plane. CLA/DCO + trademark control.

**OSS Core (Apache-2.0):**
- Framework/SDK
- Meta-model, governance primitives, plugin runtime
- SQLite default backend, embedded storage
- Python SDK, CLI
- MCP server
- All core functionality

**Proprietary Managed Plane:**
- Hosted multi-tenant servers
- Enterprise SSO/identity integration
- Scale-out storage operations
- Collaboration UI
- Audit/compliance dashboards
- Managed embeddings
- Plugin/ontology marketplace

**Governance:**
- **CLA** (Contributor License Agreement) preferred — preserves right to dual-license
- **DCO** (Developer Certificate of Origin) fallback if contributor friction matters more
- **Trademark control** on "Ontolith" name

**Defensive Option:**
- If cloud provider hosts competing managed version: fallback to **AGPL on core**
- Only triggered if concrete threat emerges
- Taxes adoption, so default stays permissive

## Rationale

**Why Apache-2.0:**
- Permissive maximizes adoption for embeddable tools
- No copyleft friction (enterprises can adopt freely)
- Compatible with proprietary derivatives (enables managed offering)
- Industry-standard for frameworks/SDKs

**Why open-core not fully OSS:**
- Sustainable business funds development
- Managed plane is clear value-add (ops, UI, scale)
- OSS core preserves self-hosting option

**Why CLA:**
- Preserves right to offer proprietary builds
- Enables relicensing if needed (e.g., defensive AGPL)
- Standard for projects with commercial backing

**Why trademark control:**
- Prevents confusing forks ("Ontolith Cloud" by competitor)
- Protects brand even with permissive license

**Why plugin seams as boundary:**
- `AuthProvider`, `StorageBackend`, `PolicyStrategy` = extension points
- Proprietary implementations slot in naturally
- Architecture and business model align

## Consequences

**Positive:**
- ✅ Frictionless adoption (permissive license)
- ✅ Clear monetization path (managed plane)
- ✅ Users own their data (can self-host core)
- ✅ Relicensing option preserved (CLA)

**Negative:**
- ⚠️ Cloud providers can host competing service
- ⚠️ CLA adds contributor friction

**Mitigations:**
- Trademark prevents brand confusion
- Defensive AGPL option if threat materializes
- CLA is standard for VC-backed OSS

## Alternatives Considered

**Fully OSS (no managed offering):**
- Rejected: Unsustainable, requires other funding

**AGPL from start:**
- Rejected: Taxes adoption, hurts growth

**Source-available (BSL, etc.):**
- Rejected: Not true OSS, limits ecosystem

**No CLA:**
- Rejected: Can't relicense if needed

## References

- PRD §12 (Licensing & business model)
- PRD §16 Decision 6
- Implementation Plan §3.5 (Governance files)
