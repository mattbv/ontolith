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
7. **`ontolith.resubmit`** — Resubmit a `changes_requested` proposal (propose, NOT write; added
   2026-07-29, see Update below, KI-027)
8. **`ontolith.retract`** — Propose retraction of an assertion (propose, NOT write; added
   2026-08-31, see Update below, KI-057/ADR-0039)

**Forbidden Tools:**
- ❌ `ontolith.write` — No direct write
- ❌ `ontolith.update` — No direct update
- ❌ `ontolith.delete` — No direct delete
- ❌ Any tool that bypasses proposal/policy pipeline

**Provenance Capture:**
- `ontolith.propose` stamps calling agent as `author` — resolved server-side from a verified
  bearer token, never a caller-supplied ID (ADR-0014)
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

## Update (2026-07-29): `ontolith.resubmit` added, KI-027

A 7th tool, **`ontolith.resubmit`**, was added (propose, NOT write) — full rationale and
implementation notes are recorded in `ADR-0022`'s own 2026-07-29 update, not duplicated here. In
short: AI proposals always route to `require_review` (ADR-0003), so an AI principal whose proposal
lands in `changes_requested` has no write capability to fall back on — without this tool, MCP was
the one interface where that state was a genuine dead end for its own author. `ontolith.resubmit`
follows the exact same shape as `ontolith.propose`: it re-runs policy evaluation and cannot write
or edit an assertion directly, so it does not widen the "no direct write" boundary this ADR
establishes — it accepts only a `proposal_id` and a token-resolved principal, the same footprint as
the tool it mirrors.

## Update (2026-08-31): `ontolith.retract` added, KI-057/ADR-0039

An 8th tool, **`ontolith.retract`**, was added (propose, NOT write) — full rationale and
implementation notes are recorded in `ADR-0039`, not duplicated here. In short: `Ontology.retract()`
had no route on any of the four shipped interfaces, so a REST/GraphQL/MCP/CLI-only deployment had
no way to retract a fact at all (KI-057). `ontolith.retract` follows the same shape as
`ontolith.propose`/`ontolith.resubmit`: it is policy-evaluated and proposal-producing, cannot write
or edit an assertion directly, and accepts only an `assertion_id`, a token-resolved principal, and
an optional `acting_as` — the same footprint as the tools it mirrors. This is a different posture
than `resolve_contradiction()`, which has no policy evaluation at all and remains deliberately
excluded from MCP as a reviewer-only action (unchanged by this update).

## References

- PRD §8 P6 (Interfaces: MCP server)
- PRD §11 (Trust & safety: AI-specific)
- PRD §16 Decision 8
- SPEC §14.4 (MCP server)
- ADR-0003 (Agent identity)
