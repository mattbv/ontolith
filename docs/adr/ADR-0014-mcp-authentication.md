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

## Update (2026-07-24): `issue_token` returns `(token, credential_id)` (closes KI-024)

`Ontology.issue_token(principal_id, author) -> str` returned only the raw token. Both of its
callers (REST's `issue_token_route`, the CLI's `principal issue-token`) then made a second,
non-transactional call — `list_tokens(principal_id, author)[0].id` — to recover the new
credential's id, relying on `get_credentials_for_principal`'s "most recent first" ordering. If a
second admin issued another token for the *same* `principal_id` in the gap between those two
calls, `[0]` could return that unrelated, newer credential instead of the one whose raw token was
just handed back — pairing the correct raw token with the wrong `credential_id` in the response. A
caller storing `(token, credential_id)` together and later revoking by `credential_id` would then
revoke the wrong credential, silently leaving the intended one active. (A narrower, same-timestamp
variant of this ordering ambiguity was already closed by the `id DESC` tiebreak recorded as an
update to ADR-0010/ADR-0022; that fix doesn't touch this cross-request race, which needs genuine
concurrent issuance, not just a coarse clock.)

Fixed by changing `issue_token`'s return type to `tuple[str, str]` — `(token, credential_id)` —
since the credential's id is already known at the point `issue_token` persists it; no second
lookup is needed. Both callers were updated to unpack the tuple directly instead of calling
`list_tokens()` afterward. This is a breaking change to `Ontology`'s public API (ADR-0019) — every
existing caller of `issue_token()` expecting a bare string breaks, including third-party code — but
was judged proportionate: the race it closes is a real, if narrow, credential-misattribution bug in
a security-sensitive path (token issuance), and no non-breaking shape existed that also removed the
redundant lookup (a two-item result object would still change every callers' unpacking pattern; a
new `issue_token_v2` method would leave the racy path reachable indefinitely).

## Update (2026-09-01, closes KI-067): `Authorization` header preferred over the `token` argument

Every tool's `token` parameter is itself a credential-exposure path this ADR never named: because
`token` is a tool *argument*, the calling model must emit it as part of every tool call — landing a
live, long-lived bearer token (this ADR's own "Negative" section: "a leaked token grants that
principal's access until revoked") in the model's own context window and in any MCP client's
tool-call logging, neither of which Ontolith controls. The SSE/streamable-HTTP transports had an
unused channel for this: the mcp SDK's own transport wiring (`ServerMessageMetadata.request_context`)
already threads the raw Starlette request through to every tool call.

**Fix:** every tool's `token` parameter became optional (`str | None = None`). A new per-server
`_bearer_token(token)` helper (`interfaces/mcp.py`) checks
`mcp.get_context().request_context.request` for an `Authorization` header first, falling back to
the `token` argument only when no header is present at all (or no HTTP request exists at all, e.g.
stdio). A header that IS present but malformed — wrong scheme, or a blank/whitespace-only value —
does NOT fall back to the argument; it fails the call closed with an `auth_error` instead. A
well-formed `Bearer <token>` header, when present, always wins — even over a `token` argument the
caller also supplied — so an SSE/streamable-HTTP deployment can omit the argument entirely and keep
the credential off the model's own context. (Round-2 review of this fix caught the original version
silently falling back to the argument on ANY unparseable header, not just an absent one — the same
exposure this fix exists to remove, reopened with no signal, the moment a proxy's header injection
ever misconfigured. Fixed before merge, not left as a follow-up.)

**Why not adopt the mcp SDK's built-in OAuth-shaped bearer-auth stack instead** (`FastMCP(auth=...,
token_verifier=...)`, `RequireAuthMiddleware`/`BearerAuthBackend`): that machinery models a full
OAuth 2.1 resource server — it requires an `issuer_url` and advertises RFC 9728 Protected Resource
Metadata pointing at a real authorization server. Ontolith has no OAuth authorization server behind
its per-principal API-key tokens (this ADR's own Decision), so wiring it up would mean either
fabricating OAuth metadata endpoints with nothing real behind them, or building a full mini
authorization server — a materially larger, out-of-scope redesign of this ADR's actual token model.
Reading the header directly keeps the existing `AuthProvider.resolve(token) -> Principal` port
completely unchanged.

**stdio residual exposure — not closed by this fix, documented instead:** stdio has no HTTP request
of any kind, so `token` remains the only channel there, unchanged from this ADR's original design.
A stdio-facing principal's token still has to be configured wherever the client launches the server
process (an environment variable or client config file, not a live tool-call argument the model
itself emits) — a materially smaller exposure than a value the model repeats into every tool call
and every transcript, but still not zero. Deployments running MCP over stdio should prefer
short-lived tokens for those principals precisely because there's no way to keep the token out of
the client's own process environment the way the header keeps it out of the *model's* context.

**Residual — the argument still works even when a header is available, so the exposure is made
avoidable, not eliminated:** an HTTP deployment cannot currently *require* the header. `token`
remains an accepted, schema-advertised argument on every tool, so a model that already has a token
in context can keep emitting it and it will keep authenticating (the header only wins when both are
present *and* well-formed). A `require_header_token: bool` flag on `create_mcp_server()` that
disables the argument fallback entirely for HTTP transports would let a deployment close this
outright — not built here, since KI-067's own Fix text scoped this pass to making the header path
available and preferred, not to removing the argument path. Left as a named follow-up rather than
silently left unaddressed.

## References

- ADR-0008 (MCP surface — this ADR implements its "server stamps `author`" provenance claim,
  which the original implementation didn't honor)
- ADR-0003 (Agent identity, delegation)
- ADR-0019 (public API stability policy — governs the breaking-change marker for the `issue_token`
  return-type change above)
- SPEC §8.2 (Authentication), §14.4 (MCP server)
- `identity/ports.py` (`AuthProvider`, stubbed since M0)
- `docs/known-issues.md` KI-024, KI-067 (both now resolved)
