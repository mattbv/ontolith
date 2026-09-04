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
return `{"error": ..., "code": "AUTH_ERROR"}` on failure (KI-059 later fixed this to read
`AuthError.code` rather than a hand-written literal — see that KI's own entry for why the literal
existed at all) — identical shape to
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

**Migration note:** a deployment sitting behind a proxy/gateway that already sets its own
`Authorization` header for an unrelated purpose (its own bearer scheme, or a foreign token) — and
that was previously relying on the `token` *argument* to authenticate to Ontolith — now fails
closed instead of authenticating via the argument, since a present header always takes priority and
a malformed or unresolvable one is never silently skipped. This is the intended tightening, not a
bug, but it is a real behavior change for that specific setup: such a deployment must strip or
rename the incoming header before proxying to Ontolith's MCP server.

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
outright — not built here: it's an opt-in hardening a deployment could reach for once available,
not a fix for a live vulnerability (the default behavior changes nothing on its own), so it was
judged out of scope for this pass rather than blocking it. Tracked as KI-073 rather than left as
ADR prose only — since built, see this ADR's own 2026-09-04 Update below.

## Update (2026-09-02, closes KI-074): one blanket error handler, not a per-tool per-type catch

KI-059 fixed MCP's error `code` *values* to read `exc.code` off the caught `OntolithError`, but
left the *mechanism* untouched: each tool still hand-caught only the specific `OntolithError`
subtypes it happened to expect (`AuthError`/`CapabilityError`/`NotFoundError`/`ValidationError`),
unlike REST's single `@app.exception_handler(OntolithError)` (`_STATUS_BY_ERROR_TYPE`) or
GraphQL's single `process_errors` override. A `SchemaError`, `PolicyDenied`, `ConflictError`,
`StorageError`, or `PluginError` escaping any tool wasn't converted to `{"error", "code"}` at all —
it propagated as an unstructured MCP protocol exception, and (for `StorageError`/`PluginError`
specifically) without the message redaction REST/GraphQL already apply to avoid leaking internal
exception text. The response shape also diverged even where the code matched: MCP returned
`{"error", "code"}` while REST/GraphQL return `{"code", "message", "detail"}` — `exc.detail` was
dropped entirely.

**Fix:** a module-level `_error_response(exc: OntolithError) -> dict[str, Any]` — the MCP
equivalent of REST's `_handle_ontolith_error`/GraphQL's `process_errors` override — redacts
`StorageError`/`PluginError` messages the same way (`isinstance`, not REST's exact-type dict
lookup, since MCP has no HTTP status to key off; matches GraphQL's own `_REDACT_MESSAGE_FOR`
precedent instead) and returns `{"error", "code", "detail"}` uniformly. Every tool now wraps its
*entire* body (after token resolution, which isn't itself an `OntolithError` — see
`_bearer_token`) in one `try: ... except OntolithError as exc: return _error_response(exc)`,
replacing every per-type `except` clause. The few remaining non-exception error paths (a
synthesized validation message, a manual `is None` "not found" check, `_bearer_token`'s own
auth-shaped strings) now construct a real exception instance (`ValidationError(...)`,
`NotFoundError(...)`, `AuthError(token_error)`) and route it through the same `_error_response()`
rather than hand-building a `{"error", "code"}` dict inline — `_error_response()` is now the single
place any tool's error dict is built, closing the shape divergence completely, not just the code
values KI-059 closed.

**Why not raise these synthesized errors instead of constructing-and-immediately-formatting them:**
raising would unwind through the same outer `try/except OntolithError` anyway (one level up in the
call stack), which works but adds no value over calling `_error_response()` directly at the point
of detection — the tool already knows it's returning early either way, and an explicit
`return _error_response(...)` is one line, not two.

**Review-driven follow-up (round 1):** the blanket handler turned a pre-existing mislabel in
`Ontology.retract()` into an information-destroying one. An unknown `assertion_id` has always
fallen through to the backend's generic `set_assertion_status`, whose "no row updated" case raises
`StorageError` — meant for a genuine storage fault on a *known-good* id, not this caller-supplied
bad-id case. Before this ADR, that `StorageError`'s message ("Assertion not found: ...") still
reached the caller unredacted, so the mislabel was cosmetic (wrong code, right text). Once
`_error_response()` started redacting every `StorageError`, the same caller-supplied bad id now got
"An internal error occurred" — a client-actionable error made opaque.

**Review-driven follow-up (round 2):** round 1's first fix checked `get_assertion(assertion_id) is
None` only inside the auto-accept write transaction, so it never ran on the review-routed path —
exactly the one an AI/MCP caller takes, since `ThresholdPolicy` always routes AI principals to
review (ADR-0003). An unknown id from an AI caller still produced no error at all: a phantom
`require_review` proposal was created and silently persisted, and if a reviewer later accepted it,
`_replay_proposal_operations` hit a bare `assert retracted is not None`, escaping as an uncaught,
blank-message `AssertionError` — worse than the redacted `StorageError` this ADR set out to fix.
Fixed properly: `retract()` now checks existence unconditionally, before a proposal is even
created or policy evaluated, raising `NotFoundError` up front regardless of how policy would route
it — matching `flag_contradiction`'s existing precedent, and safe to check once outside any
transaction since assertion existence is permanent (append-only, SPEC §5). The now-unreachable
`assert` inside `_replay_proposal_operations` was also hardened into a real `NotFoundError`, as a
backstop against any future path that might replay a proposal built some other way. This is a
behavior change on every interface that calls `retract()`, not just MCP — REST now reports an
unknown assertion as 404 rather than 500, and GraphQL's `errors` array carries `NOT_FOUND` rather
than a redacted `STORAGE_ERROR`; the CLI's plain-text output changes wording only, since it already
printed the underlying exception's message unredacted either way.

**Recorded trade-off (not a defect):** FastMCP only marks a tool call `isError=True` when the tool
*raises*; a returned `{"error", ...}` dict is a successful call carrying an error payload. Because
every tool now catches `OntolithError` itself, `SchemaError`/`StorageError`/`PluginError` — genuine
server-side faults, not just client mistakes — no longer surface as protocol-level MCP failures the
way REST's 5xx or GraphQL's `errors` array still do. This is deliberate, not an oversight: MCP tool
callers (LLM agents) are expected to read structured payloads rather than distinguish protocol-level
error frames, and matches every other tool's response shape uniformly. It does mean an agent that
only checks `isError` (rather than the response's own `code` field) will treat a redacted
`StorageError` as success — callers of MCP tools should always check `code`, not rely on
`isError` alone.

## Update (2026-09-04, closes KI-073): opt-in `require_header_token` closes the residual named above

KI-067's own Update left one residual open by name: `token` remains an accepted argument on every
tool even when a deployment would rather require the header outright, so a model that already has
a token in context can keep authenticating via the argument indefinitely — the exposure is made
avoidable, not eliminated. That Update named the exact shape of the fix (`require_header_token:
bool` on `create_mcp_server()`) and deferred it as KI-073, judged strictly opt-in hardening rather
than a live vulnerability.

**Fix:** `create_mcp_server()` gains a keyword-only `require_header_token: bool = False` parameter.
When `True`, `_bearer_token()` treats an absent (or no-request-context-at-all) header the same as
no credential supplied at all — returning "No bearer token provided" — even when the caller still
supplies a valid `token` argument, disabling the fallback entirely. A *malformed* header (KI-067's
own fail-closed case) is unaffected either way: it was never a fallback candidate to begin with.
`False` by default, since the argument fallback is what keeps stdio (no header channel exists
there) usable at all — the flag is for an HTTP (SSE/streamable-HTTP) deployment that wants to
require the private channel outright, not a change to the out-of-the-box default.

**stdio under the flag:** setting `require_header_token=True` makes stdio transports unusable
outright, exactly as anticipated when this was deferred — there is no header channel for stdio to
satisfy the requirement with. This is the documented, deliberate tradeoff, not a carve-out gap: a
deployment that needs both stdio and this flag isn't a supported configuration, and none was asked
for.

## References

- ADR-0008 (MCP surface — this ADR implements its "server stamps `author`" provenance claim,
  which the original implementation didn't honor)
- ADR-0003 (Agent identity, delegation)
- ADR-0019 (public API stability policy — governs the breaking-change marker for the `issue_token`
  return-type change above)
- ADR-0021 (REST interface — `_STATUS_BY_ERROR_TYPE`/`_handle_ontolith_error`, the pattern this
  update's `_error_response()` mirrors), ADR-0037 (GraphQL interface — `_REDACT_MESSAGE_FOR`, the
  isinstance-based redaction precedent this update follows instead of REST's exact-type lookup)
- SPEC §8.2 (Authentication), §14.4 (MCP server), §16 (stable, machine-readable error codes)
- `identity/ports.py` (`AuthProvider`, stubbed since M0)
- `docs/known-issues.md` KI-024, KI-059, KI-067, KI-073, KI-074 (all resolved)
