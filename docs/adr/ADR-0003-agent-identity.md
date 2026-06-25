# ADR-0003: Agent Identity Model

**Status:** Accepted

**Date:** 2026-06-20

**Deciders:** Ontolith Core Team

## Context

AI agents are first-class authors in Ontolith. We need to model their identity in a way that:
- Makes every agent-authored fact attributable to a responsible human/team
- Captures which model+version produced each assertion (agents evolve)
- Supports delegation chains (agent acting on behalf of a user)
- Enables service-based authentication (OIDC client-credentials, workload identity)

## Decision

**Service principal + mandatory accountable owner + per-assertion model capture + service auth + delegation/acting-as.**

**Agent as Principal:**
- `kind = "ai"` (vs `human` or `service`)
- Identified by slug (e.g., `"scout-agent"`)
- Service-based auth: OIDC client-credentials or workload identity

**Mandatory Accountable Owner:**
- `owner` field is **required** for `kind="ai"`
- Points to a human or team principal
- Enforced in code AND database (`CHECK (kind <> 'ai' OR owner IS NOT NULL)`)

**Per-Assertion Model Capture:**
- `model` field on every assertion by an AI principal
- Captures model family + version (e.g., `"claude-sonnet-4.5"`)
- Attribution survives model changes (agent identity is stable, model varies)

**Delegation / Acting-As:**
- Optional `acting_as` field on assertions and proposals
- Records the human who triggered the agent
- Provenance includes both: agent (author) + human (acting_as)

## Rationale

**Why mandatory owner:**
- Always a responsible human accountable for agent facts
- Enables trust delegation (owner vouches for agent)
- Supports revocation (disable agent, not every assertion)

**Why per-assertion model capture:**
- Agent may upgrade models over time
- Debugging requires knowing which model version produced a fact
- Enables model-based trust calibration

**Why service identity:**
- Agents run as services (not interactive logins)
- OIDC client-credentials / workload identity are standard
- No long-lived API keys in v1 (security)

**Why acting-as:**
- Preserves user context (agent acting for Alice vs Bob)
- Supports audit ("Alice triggered agent X to propose Y")
- Enables per-user policy (agent inherits user's trust level)

## Consequences

**Positive:**
- ✅ Every agent fact is attributable to an owner
- ✅ Model evolution is traceable
- ✅ Delegation chain is explicit in provenance
- ✅ Standard service auth (OIDC)

**Negative:**
- ⚠️ Agents require owner setup (not fully autonomous)
- ⚠️ Model capture requires plumbing at assertion time

**Mitigations:**
- Owner requirement is enforced (can't be skipped)
- SDK automatically captures model from context

## Alternatives Considered

**Agent as sub-principal of owner:**
- Rejected: Loses agent identity if owner changes

**Model as agent attribute:**
- Rejected: Agents upgrade models; per-assertion capture is correct

**No acting-as:**
- Rejected: Loses user context, breaks per-user policy

## References

- PRD §7 (Core concepts: Principals)
- PRD §16 Decision 3
- SPEC §8 (Principal & identity model)
- SPEC §5.3 (Assertion: model field)
