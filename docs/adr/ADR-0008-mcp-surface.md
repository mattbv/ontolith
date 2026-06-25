# ADR-0008: MCP Surface (Read + Propose, No Write)

**Status:** Accepted

**Date:** 2026-06-20

**Deciders:** Ontolith Core Team

## Context

The MCP (Model Context Protocol) server exposes Ontolith to AI agents as a tool. We need to decide:
- Which operations agents can perform directly
- Whether to expose direct write capability
- How to enforce the governance model

## Decision

**Read/query + propose + flag_contradiction + provenance. No direct write.**

**Exposed Tools:**

1. **`ontolith.schema`** — Read concepts/relations/temporality (read)
2. **`ontolith.query`** — Symbolic + semantic retrieval (read)
3. **`ontolith.get`** — Fetch entity + current assertions (read)
4. **`ontolith.provenance`** — Provenance projection for any assertion (read)
5. **`ontolith.propose`** — Create a proposal (propose, NOT write)
6. **`ontolith.flag_contradiction`** — Open/extend contradiction (propose, NOT write)

**Forbidden Tools:**
- ❌ `ontolith.write` — No direct write
- ❌ `ontolith.update` — No direct update
- ❌ `ontolith.delete` — No direct delete
- ❌ Any tool that bypasses proposal/policy pipeline

**Provenance Capture:**
- `ontolith.propose` stamps calling agent as `author`
- Captures `model` from agent context
- Records `acting_as` if delegation present

**Policy Decides:**
- Agent capability defaults to `propose`
- Policy engine evaluates every proposal
- Review gate enforced per policy (confidence, trust, source)

## Rationale

**Why no direct write:**
- Agents default to `propose` (governance model, ADR-0003)
- Write bypasses review (only for high-trust principals with explicit grant)
- MCP agents are untrusted by default (external to system)
- Prevents accidental/malicious silent overwrites

**Why propose instead:**
- Governance model is preserved (proposal → policy → review)
- Agent writes are auditable (provenance trail)
- Policy can auto-accept high-confidence proposals
- Review gate stops hallucinated facts

**Why read/query:**
- Grounding requires retrieval
- Read is safe (no side effects)
- Provenance lookup aids agent reasoning

**Why flag_contradiction:**
- Agents can surface disagreements (valuable signal)
- Contradiction creation is proposal-like (doesn't bypass governance)
- Helps maintain KB quality

## Consequences

**Positive:**
- ✅ Governance model enforced (no write bypass)
- ✅ Agent writes are reviewable
- ✅ Safe default (propose, not write)
- ✅ Provenance captures agent + model

**Negative:**
- ⚠️ High-latency for auto-accepted proposals (could optimize)
- ⚠️ Agents can't "just write" (requires proposal/accept)

**Mitigations:**
- Policy auto-accepts high-confidence proposals from trusted agents
- Proposal → accept is single transaction (latency minimized)
- Elevated agents can get `write` capability (explicit grant)

## Alternatives Considered

**Expose write tool:**
- Rejected: Bypasses governance, silently overwrites facts

**Expose write with policy check:**
- Rejected: Policy checks belong in proposal path, not tool layer

**No propose (read-only):**
- Rejected: Agents can't contribute knowledge

## References

- PRD §8 P6 (Interfaces: MCP server)
- PRD §11 (Trust & safety: AI-specific)
- PRD §16 Decision 8
- SPEC §14.4 (MCP server)
- ADR-0003 (Agent identity)
