# ADR-0039: `Ontology.retract()` Gains REST/GraphQL/CLI/MCP Routes, All at `propose` Tier

**Status**: Accepted
**Date**: 2026-08-31
**Deciders**: Ontolith Core Team
**Related**: SPEC §5.3 (append-only assertions, retraction), §9 (proposal/policy pipeline), §10.3 (contradiction resolution), §14 (interfaces), ADR-0008 (MCP tool surface), ADR-0003 (delegation), ADR-0021/0022 (REST write/review/admin), ADR-0037 (GraphQL interface), KI-002 (original `retract()`), KI-033/KI-043/KI-044/KI-051 (retract's contradiction guards), KI-057

---

## Context

`Ontology.retract()` is the only production entry point for governed retraction — introduced by KI-002 specifically so retraction would go through the same proposal/policy/conflict-routing pipeline as every other write, then hardened across five further KIs (KI-033's self-dealing guard, KI-043's capability floor and review routing, KI-044's terminal-status winner guard, KI-051's terminal-member guard extension). Despite that governance investment, it had no route on any of the four shipped interfaces (KI-057) — reachable only via `WriteView.retract()`/the SDK directly, meaning a REST/GraphQL/MCP/CLI-only deployment (the normal production shape) had no way to retract a fact at all.

`retract()` itself is mechanically a `propose`-tier action: it constructs a `Proposal`, runs it through `self.policy.evaluate(...)`, and only writes if the decision is `AutoAccept` — identical shape to `propose()`/`propose_ref()`, not a direct write like `assert_literal()`/`assert_ref()`. This distinguishes it from `resolve_contradiction()`, which is explicitly reviewer-only (requires `review`/`admin` capability unconditionally, no policy evaluation, no proposal object) and — per KI-009's resolution note — deliberately excluded from MCP's surface as "out of scope for the ADR-0008 propose-only surface."

KI-057's own Fix text named the open question directly: wire `retract()` into REST/GraphQL/CLI, and *decide* whether MCP should also expose it — either at `propose` tier (like `flag_contradiction`) or excluded (like `resolve_contradiction`).

## Decision

**Expose `retract()` on all four interfaces, including MCP, at `propose` tier.**

- **REST**: `POST /assertions/{assertion_id}/retract` — `acting_as` as an optional query parameter (mirroring `GET /contradictions`'s `state` query param, since the only other field is `assertion_id` itself, already in the path). Reuses the existing `ProposeOut`/`ProposalOut` response models unchanged, since `retract()`'s return shape (`Proposal`, `Decision`) is identical to `propose()`'s.
- **GraphQL**: `Mutation.retract(assertionId, actingAs)` — reuses `ProposeResultType`, dispatched through `run_in_threadpool` like every other mutation (KI-052).
- **CLI**: new top-level `ontolith retract <assertion_id> --author <id> [--acting-as <id>]` command, alongside `assert`/`assertions`/`query`/`reindex`. Not nested under `assert` (`assert` is a plain `@app.command`, not a Typer sub-app — turning it into a group would rename an existing command and break scripts calling `ontolith assert ...` directly).
- **MCP**: `ontolith.retract(assertion_id, token, acting_as=None)` — same shape as `ontolith.propose`/`ontolith.resubmit`, resolves the acting principal from `token` (ADR-0014), never a caller-supplied ID.

**Rationale for including MCP** (the one genuinely open question): `resolve_contradiction()`'s MCP exclusion is about *tier*, not about contradictions specifically — it's a reviewer-only action with no policy evaluation, the same reason `accept_proposal`/`reject_proposal`/`request_changes` also have no MCP tool. `retract()` doesn't share that shape: it's evaluated by `self.policy` exactly like `propose()`, can be auto-accepted, required-review, or rejected depending on the calling principal's trust, and an AI-kind principal retracting a contradiction member still gets routed through the same review-floor/party-guard machinery `accept_proposal()` re-checks (KI-043). Excluding it from MCP while including `flag_contradiction` — a structurally identical propose-tier contradiction action — would draw an inconsistent line with no capability-model justification. ADR-0008's own "why flag_contradiction" rationale ("agents can surface disagreements," "doesn't bypass governance") applies just as directly to an agent proposing that a fact should be retracted.

## Rationale

**Why REST uses a query parameter instead of a request body for `acting_as`:** every other field this route needs (`assertion_id`) is already in the URL path; adding a `RetractIn` body model with a single optional field would be more ceremony than `list_contradictions_route`'s existing `state` query-param precedent for a similarly single-optional-field route.

**Why CLI doesn't take a bearer token like MCP/REST/GraphQL:** every existing CLI command (`assert`, `proposal accept`, etc.) resolves the acting principal from a plain `--author <id>` option, not a token — CLI access is already file-level trusted (the same rationale ADR-0038 records for `principal create`'s bootstrap exception). `retract` follows that existing convention rather than introducing token-based auth for one command.

**Why the response reuses `ProposeOut`/`ProposeResultType` unchanged:** `retract()`'s return type (`tuple[Proposal, Decision]`) is identical to `propose()`'s — REST's `create_proposal_route` and GraphQL's `propose` mutation already define exactly this shape; a new type would duplicate fields with no new information.

## Consequences

**Positive:**
- Closes KI-057 — retraction is now reachable from every shipped interface, not just the SDK.
- No new domain logic, no new `StorageBackend` port method — every route is a thin wrapper calling the already-governed, already-hardened `Ontology.retract()`.
- Consistent capability posture: MCP's `ontolith.retract` sits at the same `propose` tier as `ontolith.propose`/`ontolith.flag_contradiction`/`ontolith.resubmit`, no new tier introduced.

**Negative / follow-ups:**
- An AI principal can now request retraction of any assertion via MCP (subject to the same policy evaluation and contradiction guards every other retraction path already enforces) — this is a new *reachable* capability, not a new *permitted* one, since `WriteView.retract()`/the SDK could already do this; the risk surface is "more callers can reach an already-governed action," not "a new ungoverned action exists."
- CLI's `retract` command has no conformance vector (matching `schema migrate`'s ADR-0034 precedent) — covered by `tests/unit/test_cli.py` unit tests instead, since it's a thin wrapper with no new backend/domain behavior.
- KI-059 (MCP error-code drift from REST/GraphQL) is not addressed by this ADR — `ontolith.retract` reuses the same `auth_error`/`capability_error`/`not_found`/`validation_error` codes `ontolith.resubmit` already uses, matching existing (drifted) MCP convention rather than introducing yet another inconsistency, but not fixing the underlying drift either.

## Alternatives Considered

- **Exclude MCP, matching `resolve_contradiction`'s posture**: rejected — `retract()` is structurally a `propose`-tier action (policy-evaluated, proposal-object-producing), not a reviewer-only terminal action like `resolve_contradiction()`. Excluding it would be inconsistent with `flag_contradiction`'s existing MCP exposure, which shares the same tier and a similar contradiction-adjacent surface.
- **CLI `assert retract <id>` (nested under `assert`, as KI-057's Fix text originally suggested)**: rejected — `assert` is registered as a plain `@app.command("assert")`, not a `typer.Typer()` sub-app; converting it into a group to nest `retract` underneath would change `ontolith assert <subject> <predicate> <value>`'s invocation shape, a breaking rename of an existing, unrelated command. A new top-level `ontolith retract` command avoids that.
- **REST request body instead of a query parameter for `acting_as`**: rejected — see Rationale; a single optional field doesn't warrant a new Pydantic model when an existing query-param precedent (`GET /contradictions`) already covers the shape.
