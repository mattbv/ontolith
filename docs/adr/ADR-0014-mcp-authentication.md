# ADR-0014: MCP Authentication — Per-Principal API Keys

**Status:** Accepted

**Date:** 2026-07-07

**Deciders:** Ontolith Core Team

## Context

ADR-0008 decided the MCP server's tool surface (read/query/propose/flag_contradiction/provenance,
no direct write) and stated that `ontolith.propose` "stamps calling agent as `author`". In the
shipped implementation, `author` was instead a plain caller-supplied tool argument: `Ontology.propose`
only checked that the named principal *existed* (`backend.get_principal(author)`), never that the
caller was actually entitled to act as that principal. Any MCP client could name any registered
principal ID — including a `write`-capable human (human IDs are emails, often guessable) — and be
evaluated with that principal's full trust and capability. Combined with the delegation bug fixed
alongside this one (`ThresholdPolicy` substituting the delegate's capability wholesale), this made
the "no direct write tool" guarantee bypassable: `propose` functioned as an unauthenticated write
when the caller picked a sufficiently trusted `author`.

`AuthProvider` (`identity/ports.py`) existed only as an empty stub since M0, with a comment
deferring the real interface to M1 — M1 shipped without it ever being implemented, and nothing
in `mcp.py` used it.

## Decision

**Per-principal API-key tokens**, not a single server-bound principal and not a shared-secret
allow-list.

- Each principal is issued a token via `Ontology.issue_token(principal_id, author)` (also exposed
  via `ontolith principal issue-token --author <admin>` in the CLI). Issuing, revoking, and
  listing credentials all require the `author` to hold `admin` capability — minting a bearer
  token converts local access into a remote, network-reachable credential, a higher-stakes action
  than the target principal's own capability level. The raw token is returned exactly once; only
  its SHA-256 hash is persisted (`principal_credential` table). A principal may hold multiple
  concurrent active tokens — rotation is issue-new-then-revoke-old, both explicit.
- `AuthProvider.resolve(token) -> Principal` is now a real port method. The concrete
  `TokenAuthProvider` (`identity/token_auth.py`) implements it against the abstract
  `StorageBackend` port (`get_principal_by_token_hash`), keeping `identity/` free of any concrete
  adapter import — same pattern `Ontology` itself uses.
- `create_mcp_server(kb, auth_provider)` takes the `AuthProvider` as a required argument.
  `ontolith.propose` and `ontolith.flag_contradiction` now take a `token` parameter instead of
  `author`; the acting principal is always resolved server-side from the verified token, never
  taken as a caller-supplied ID. `acting_as` (delegation target) remains a plain tool argument —
  it names who the *resolved* principal wants to act as, still authorization-checked against
  `principal.owner` inside `Ontology.propose`/`retract`, and now capped by
  `min(capability(author), capability(acting_as))` (see the companion delegation fix).

**Update (2026-07-22, closes KI-021):** the four read tools (`ontolith.schema`,
`ontolith.get`, `ontolith.query`, `ontolith.provenance`) shipped in the original
implementation of this ADR with no `token` parameter at all — a gap distinct from the
spoofing vulnerability above, since there was no capability to spoof, only a missing
`AuthProvider.resolve()` call. It went unnoticed until the REST interface (ADR-0021)
was scoped and, per SPEC §8.3 ("`read`/`query`: required for any retrieval"), required
auth on its own read routes — making MCP's gap visible by direct comparison. All four
read tools now take `token: str`, resolve it via the same `AuthProvider.resolve()`, and
return `{"error": ..., "code": "auth_error"}` on failure — identical shape to
`ontolith.propose`'s existing auth-failure path. No new capability-tier logic was
needed: capability is a total order (`read < propose < write < review < admin`, SPEC
§8.3), so any principal a token resolves to already clears the "read" floor —
authentication *is* the capability check for these four tools, same reasoning ADR-0021
applied to REST's read routes.
- One server process (one `AuthProvider`/backend pair) can serve many principals, each
  authenticated by their own token — this was the explicit reason per-principal tokens were chosen
  over a single server-bound identity.

## Rationale

**Why not bind the MCP server to one operator-configured principal at startup:**
Rejected as the first design in this ADR's drafting. It closes the spoofing hole but forces one
server process per distinct agent identity, which doesn't match how this system is actually meant
to be used — multiple AI principals, each with their own trust level and owner, sharing one KB
connection.

**Why not a full OIDC/workload-identity `AuthProvider` now:**
That is real, larger authentication infrastructure — a token issuer, a trust anchor, credential
lifecycle beyond issue/revoke. `AuthProvider` was stubbed for exactly this in M0 and never
delivered; this ADR fills the port with a minimal, real implementation (a verified credential
lookup, not a caller-asserted ID) rather than leaving the gap open further while waiting for the
full version. Full OIDC/workload-identity support remains explicitly future work.

**Why SHA-256, not bcrypt/scrypt/argon2, for the token hash:**
The input is already a high-entropy random secret (`secrets.token_urlsafe(32)`, 256 bits), not a
low-entropy human password. Slow password-hashing algorithms exist to resist brute-forcing a small
search space; they buy nothing here and would add latency to every authenticated MCP call.

**Why `secrets.token_urlsafe` instead of the injected `IdProvider`:**
`IdProvider`/`Clock` are banned from ad hoc use in domain logic for *reproducibility* — tests need
deterministic IDs/timestamps. A bearer token's entire value is cryptographic unpredictability,
which is the opposite property; using `IdProvider` here would produce guessable tokens. This is a
deliberate, narrow, documented exception, not a violation of the determinism rule. The credential
row's own `id`/`created_at` still go through `id_provider`/`clock`.

## Consequences

**Positive:**
- ✅ Closes the CRITICAL finding: `author` can no longer be spoofed by naming any registered
  principal ID — it is always derived from a verified credential.
- ✅ One server supports many principals (matches the multi-agent-owner model), unlike a
  single-bound-principal design.
- ✅ Fills the `AuthProvider` stub with a real (if intentionally minimal) implementation instead
  of leaving it empty.

**Negative:**
- ⚠️ Still not full OIDC/workload-identity — a leaked token grants that principal's access until
  revoked; there's no short-lived-credential story yet.
- ⚠️ Token issuance/revocation is an operator action (CLI/SDK), not self-service — acceptable for
  the current trust model (operator provisions agents) but will need revisiting for a
  multi-tenant hosted deployment.

**Mitigations:**
- `revoke_token`/`principal revoke-token` exist now, so a leaked token can be invalidated
  immediately without touching the principal record itself.
- The `principal_credential` schema (separate table, `revoked_at` nullable, never deleted) is
  designed to extend to expiry timestamps or per-token scoping later without a breaking migration.

## Alternatives Considered

**Single server-bound principal (config-time `--author`):**
Rejected — see Rationale. Too inflexible for multi-agent deployments.

**Shared secret + principal allow-list:**
Simpler (no credential table), but every caller shares one secret — no per-principal revocation,
no way to tell which agent made a call from the credential alone. Rejected in favor of
attributable, individually revocable per-principal tokens.

**Leave `author` caller-supplied, add a warning in docs:**
Rejected outright — this is the vulnerability being fixed, not a documentation gap.

## References

- ADR-0008 (MCP surface — this ADR implements its "server stamps `author`" provenance claim,
  which the original implementation didn't honor)
- ADR-0003 (Agent identity, delegation)
- SPEC §8.2 (Authentication), §14.4 (MCP server)
- `identity/ports.py` (`AuthProvider`, stubbed since M0)
