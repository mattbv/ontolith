# ADR-0037: GraphQL Interface

**Status:** Accepted

**Date:** 2026-08-27

**Deciders:** Ontolith Core Team

**Related:** ADR-0021/ADR-0022 (REST interface), ADR-0014 (MCP bearer-token
authentication), ADR-0008 (MCP tool surface)

## Context

M3's scope column named two unstarted items: the RDF/OWL bridge (shipped in
ADR-0036) and a GraphQL interface. SPEC §14.3 defines GraphQL narrowly and
separately from REST's full resource list:

> GraphQL exposes `Entity`, `Assertion`, `Proposal`, `Contradiction`,
> `Principal` types with `query`, `propose`, and `review` operations
> mirroring the SDK.

This is a materially smaller surface than REST's, which — beyond SPEC parity
— also grew a direct-write route and principal/token admin as REST-specific
extensions (ADR-0022, KI-022). `pyproject.toml` already carried an unused
`graphql = ["strawberry-graphql>=0.219"]` optional-dependency group reserved
for exactly this, dating back to M0's repo skeleton.

## Decision

**1. Scope: query, propose, and review only — no direct write, no principal
admin.** `Query` exposes `schema`, `entity`, `query`, `provenance`,
`proposals`, `contradictions`, and `principals` (read-only — `Principal` is
one of SPEC's five named types, but SPEC's operation list doesn't include
principal creation). `Mutation` exposes `propose`, `acceptProposal`,
`rejectProposal`, `requestChanges`, `resubmitProposal`, `flagContradiction`,
and `resolveContradiction` — every SDK-level propose/review operation REST
already wraps, deliberately excluding REST's `POST /assertions` (direct
write) and `POST /principals`/token routes (admin). Widening this surface
later, if ever needed, is additive; narrowing an already-shipped GraphQL
schema is a breaking change for any client, so starting at SPEC's literal
scope was the safer default.

**2. Module shape mirrors `interfaces/rest.py`:** one file,
`src/ontolith/interfaces/graphql.py`, one factory,
`create_graphql_app(kb: Ontology, auth_provider: AuthProvider, name: str =
"ontolith", *, graphql_ide=...) -> FastAPI`, mounting a
`strawberry.fastapi.GraphQLRouter` at `/graphql`. Strawberry types are
defined in the same file, same as REST's Pydantic models.

**3. Auth: reuse ADR-0014 bearer tokens, required on every query and
mutation — but resolved differently than REST's `Depends`-based gate.**
REST's `_resolve_principal` dependency raises before a route body ever
runs. GraphQL has no equivalent single request-level gate that wouldn't
also block introspection (`{ __schema { ... } }`) — and introspection
needs to stay reachable unauthenticated, the same posture as REST's
`/docs`/`/openapi.json` (ADR-0021 §2: exposes the API's shape, not its
data). So `context_getter` resolves the bearer token once per request and
stores either the resolved `Principal` or the `AuthError` in
`info.context` — never raising there — and every resolver calls
`_require_principal(info)` first, which raises the stored error if
resolution failed. The exception then flows through GraphQL's normal
per-field error path into the response body, landing in the same place a
domain error from deeper in the call stack would.

**4. Error handling: one centralized hook, not per-resolver try/except —
the same "one mapping" principle ADR-0021 established for REST, adapted to
GraphQL's different error channel.** GraphQL has no HTTP-status channel for
domain errors: by convention every response is HTTP 200, with errors
surfacing in the response body's `errors[]` array. A custom
`_OntolithSchema(strawberry.Schema)` overrides `process_errors` (the
official hook `strawberry.Schema` exposes for exactly this) — for every
error whose `original_error` is an `OntolithError`, it sets `extensions =
{"code": ..., "detail": ...}`; `StorageError`/`PluginError` (the same two
REST maps to 5xx) additionally get their message redacted to a generic
string, with the real message logged server-side only, same rationale as
REST's redaction. Anything that isn't an `OntolithError` (a genuine
programming bug) falls through to the default `process_errors` behavior
(log-only) rather than being mistaken for a domain error — deliberately not
fabricating a `code` extension for it.

**5. Filters use an explicit `[FilterInput!]` list, not a JSON scalar.**
REST's `QueryIn.filters` is a `dict[str, str]`; GraphQL has no native map
type. Rather than adding a `JSON` scalar (a broader escape hatch than this
query surface needs), `FilterInput { key: String!, value: String! }` keeps
filters typed and self-documenting in the schema, at the cost of a more
verbose call shape than a bare dict.

**6. `EntityType.assertions` is a nested, lazily-resolved field, not a
flat container the way REST's `EntityDetailOut{entity, assertions}` is.**
This is the one place this interface diverges from mechanically mirroring
REST's response shape — GraphQL's whole value proposition is
client-selected nested fetching, so `entity(id) { id assertions { ... } }`
only touches `kb.backend.assertions()` when a caller actually asks for
them (verified by a dedicated test that patches `assertions()` to track
calls and confirms it's never invoked when the field isn't requested).
Every other type is a flat projection of its REST counterpart.

**7. `pyproject.toml`'s `graphql` extra widened to
`strawberry-graphql[fastapi]>=0.219` plus `uvicorn>=0.27`.** The bare
`strawberry-graphql` dependency doesn't pull in `fastapi` —
`strawberry.fastapi.GraphQLRouter` needs it, and previously it was only
reachable by also installing `ontolith[rest]` or `[all]`. Mirrors REST's
own self-sufficient extra (`rest = ["fastapi>=0.110", "uvicorn>=0.27"]`) so
`pip install ontolith[graphql]` alone is enough to run this interface.

## Rationale

**Why SPEC's literal scope instead of REST parity:** SPEC names GraphQL's
operation set explicitly and separately from REST's ("query, propose, and
review... mirroring the SDK"), and REST's own extra routes are documented
in ADR-0022 as deliberate REST-specific extensions beyond SPEC, not a
baseline every interface should match. Matching REST's full surface here
would mean re-deciding, in GraphQL's schema, exactly the same
direct-write/admin exposure question ADR-0021/0022 already settled once —
with no new information to justify a different answer.

**Why `process_errors` instead of wrapping every resolver:** Strawberry's
`Schema.process_errors` is the one hook that sees every field error
regardless of which resolver raised it (graphql-core catches per-field
exceptions and appends them to the result before this hook runs), making
it the direct GraphQL analog of REST's single
`@app.exception_handler(OntolithError)` — a decorator applied to N
resolvers would have been N places to keep in sync instead of one.

**Why defer `context_getter`-time auth instead of matching REST's model
exactly:** the alternative (raising `AuthError` inside `context_getter`)
would return a transport-level failure for the entire request, including
introspection — the one behavior REST doesn't have a direct analog for,
since REST's docs endpoints are separate routes from its data routes.
GraphQL bundles both under one endpoint, so the auth gate has to live
inside field resolution, not in front of it.

## Alternatives Considered

**A `JSON` scalar for `Query.query`'s filters, rejected.** Would let a
caller pass an arbitrary nested JSON value where only flat string equality/
operator filters are ever interpreted (`QueryBuilder.where`'s existing
`__contains`/`__gt`/etc. suffix convention, KI-039) — a wider surface than
the underlying capability, for no expressiveness gain over an explicit
`[FilterInput!]` list.

**Matching REST's exact response shapes (flat `EntityDetailOut`-style
containers) throughout, rejected for `EntityType.assertions`.** Considered
for consistency with every other type in this module, but would give up
GraphQL's core advantage (client-selected, lazily-resolved nested fields)
for a surface whose entire reason for existing alongside REST is that
advantage.

**Widening scope to match REST's write/admin routes now, rejected.** See
Rationale — no new information since ADR-0021/0022 to justify revisiting
that boundary, and narrowing a shipped GraphQL schema later is harder than
widening one.

## Consequences

**Positive:** SPEC §14.3's GraphQL requirement is met exactly as specified.
Zero new auth infrastructure (ADR-0014's tokens, unchanged). Centralized
error mapping, same principle as REST. `EntityType.assertions`
demonstrates the interface's actual value over REST for at least one field.

**Negative / follow-ups:** No direct-write or principal-admin mutations —
a GraphQL client needs REST or the SDK/CLI for those, by design (Decision
§1). No subscriptions (SPEC doesn't ask for any, and `strawberry`'s
subscription support wasn't evaluated). No pagination on `Query.query`
beyond `limit` (REST doesn't have one either, KI-022, and this interface
doesn't reopen it — offset pagination is one `QueryBuilder`-level addition
either interface will inherit together, not decided per-interface).
`Query.principals`' admin gate is enforced entirely inside
`Ontology.list_principals`, same as REST — a regression there would affect
both interfaces identically, which is the intended shared-gate behavior,
not a gap specific to this ADR.

M3's scope column is now fully closed: REST (ADR-0021/0022), hybrid
retrieval (KI-018), the RDF/OWL bridge (ADR-0036), and GraphQL (this ADR)
are all shipped.

## References

- SPEC §14.3 (REST + GraphQL), §16 (error model), §8.3 (capabilities), §17
  (security model)
- ADR-0021 (REST read + propose slice), ADR-0022 (REST write/review/admin
  slice — names GraphQL as the one remaining M3 gap this ADR closes),
  ADR-0014 (MCP bearer-token authentication), ADR-0008 (MCP tool surface —
  the same read/propose-first precedent this ADR's scope follows)
- `docs/known-issues.md` KI-022 (REST's own deferred-scope list, referenced
  for what this ADR deliberately does not add)
