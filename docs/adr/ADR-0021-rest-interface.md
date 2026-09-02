# ADR-0021: REST Interface — Read + Propose Slice

**Status:** Accepted

**Date:** 2026-07-21

**Deciders:** Ontolith Core Team

## Context

SPEC §14.3 defines a REST resource set spanning every capability tier:
`/namespaces`, `/entities`, `/assertions`, `/proposals` (create +
`/{id}/accept|reject|review`), `/contradictions/{id}/resolve`, `/principals`,
`/query`, `/provenance/{assertion_id}`. None of it existed before this ADR —
`src/ontolith/interfaces/` contained only `cli.py` and `mcp.py`.
`pyproject.toml` already carried an unused `rest = ["fastapi>=0.110",
"uvicorn>=0.27"]` optional-dependency group reserved for exactly this, dating
back to M0's repo skeleton. Tracked as KI-022.

The full SPEC §14.3 resource set spans read, propose, write, and
review/admin capability tiers in one surface — a materially bigger jump than
MCP's deliberately narrow read/propose-only tool set (ADR-0008). This ADR
scopes the first REST PR to a read + propose slice mirroring MCP's proven
6-tool surface; direct write, review/admin actions, and contradiction
resolution are deferred to a follow-up PR (see KI-022's remaining scope
list).

## Decision

**1. Module shape.** `src/ontolith/interfaces/rest.py` (single file) exposes
one factory, `create_rest_app(kb: Ontology, auth_provider: AuthProvider,
name: str = "ontolith") -> FastAPI`, mirroring `mcp.py`'s
`create_mcp_server` shape exactly. Pydantic request/response models are
defined in the same file.

**2. Auth: reuse ADR-0014 bearer tokens, required on every route including
reads.** Every route depends on a `_resolve_principal` dependency that reads
`Authorization: Bearer <token>`, resolves it via the same `AuthProvider`
port / `TokenAuthProvider` implementation MCP already uses, and raises the
domain `AuthError` (not a bare `HTTPException`) on a missing/malformed
header or an invalid/revoked token — letting the single error-mapping
handler below produce a consistent response body for every failure mode,
auth included. A principal issued a token via `Ontology.issue_token()` can
use it against both MCP and REST unchanged.

Requiring auth on reads is a deliberate divergence from the *shipped* MCP
server, whose `schema`/`get`/`query`/`provenance` tools currently take no
token at all. SPEC §8.3 states "`read`/`query`: required for any retrieval"
and §17 requires every operation to be capability-checked against a
resolved principal — REST's read routes satisfy that from the start; MCP's
current gap is tracked separately as KI-021, to be closed in the same spirit
once picked up, rather than fixed twice from two different starting
assumptions.

No separate capability-check layer is needed for the read routes:
capability is a total order (`read < propose < write < review < admin` —
SPEC §8.3) and every principal has at least floor-level `read`, so a
successfully-resolved principal always clears a "read" gate —
authentication *is* the capability check for `/schema`, `/entities/{id}`,
`/query`, `/provenance/{id}`, and `GET /proposals`. `POST /proposals` needs
no extra gate either: `Ontology.propose()`/`propose_ref()` already perform
their own capability check via policy evaluation and raise
`CapabilityError`/`PolicyDenied` on failure, exactly as MCP relies on today.

This means there is no per-principal or per-namespace read scoping in this
slice: any authenticated principal can read any entity, assertion,
provenance record, or proposal in the namespace, including other
principals' `source`/`rationale` text via `GET /proposals` — surfaced by
`security-reviewer`'s pass on this ADR's first implementation. This is the
direct consequence of SPEC §8.3's global `read` capability, not an
oversight, but `GET /proposals` is worth naming explicitly since it has no
MCP-tool precedent to inherit the same posture implicitly from.

`create_rest_app` also forwards `docs_url`/`redoc_url`/`openapi_url` to
`FastAPI(...)` (defaulting to FastAPI's own docs-enabled behavior).
FastAPI serves `/docs`/`/redoc`/`/openapi.json` unauthenticated by
default, exposing the API's shape (not its data); a deployment that wants
those closed passes `docs_url=None, redoc_url=None, openapi_url=None`
rather than this factory deciding unilaterally.

**3. Error handling: one mapping, not per-route try/except.** A single
`_STATUS_BY_ERROR_TYPE: dict[type[OntolithError], int]` registered via
`@app.exception_handler(OntolithError)` maps every error subtype to an HTTP
status and a body `{"code": exc.code, "message": exc.message, "detail":
exc.detail}` (SPEC §16). This is a strict improvement over `mcp.py`'s
per-call-site duplication (`{"error": str(exc), "code": exc.code}` repeated
at each tool — KI-059 fixed the *code value* to match this table, but the
duplication itself, and `.detail` being dropped entirely, remain; KI-074
tracks closing that gap the same way REST/GraphQL already have).

| Error | HTTP status |
|---|---|
| `ValidationError` | 400 |
| `SchemaError` | 400 |
| `AuthError` | 401 |
| `CapabilityError` | 403 |
| `PolicyDenied` | 403 |
| `ConflictError` | 409 |
| `NotFoundError` | 404 |
| `StorageError` | 500 |
| `PluginError` | 500 |

A second handler, registered for FastAPI's own `RequestValidationError`
(malformed request bodies caught by Pydantic before a route body ever
runs — e.g. a missing required field), maps onto the same envelope with
`code="VALIDATION_ERROR"`, status 400, and `detail={"errors": [...]}`
(Pydantic's own structured error list) — otherwise this class of failure
would surface FastAPI's default 422 body shape instead of the SPEC §16
envelope, undermining "one mapping for every failure."

**4. Endpoints (read + propose slice).** Direct mapping from MCP's 6 tools,
adapted to REST verbs, plus one read-only addition (`GET /proposals`,
already on `Ontology` — used by the CLI's `proposal list` — with no MCP
tool equivalent):

| Route | Method | Mirrors |
|---|---|---|
| `/schema` | GET | `ontolith.schema` |
| `/entities/{entity_id}` | GET | `ontolith.get` |
| `/query` | POST | `ontolith.query` |
| `/provenance/{assertion_id}` | GET | `ontolith.provenance` |
| `/proposals` | POST | `ontolith.propose` |
| `/proposals` | GET | *(new)* |

`/query` is POST, not GET: `filters` is an arbitrary dict and `semantic` is
free text, neither of which encodes cleanly into a query string. `/query`'s
request body has no `namespace` field — `Ontology.namespace` is hardcoded to
`"default"` (M1 limitation, `ontology.py`'s own comment), so `kb.query()`
takes no namespace argument to forward one to. (At the time this ADR was
written, `ontolith.query`'s MCP tool still accepted a `namespace` parameter
that never actually did anything — that pre-existing no-op was removed by
ADR-0043/KI-058, bringing MCP in line with what this ADR already did here.)
`/schema`'s `namespace` query parameter *is* real
(`StorageBackend.get_schema(namespace)` is namespace-scoped at the backend
level, independent of `Ontology.namespace`), so it's kept.

`value`/`value_type` and `target` on `POST /proposals` remain mutually
exclusive (XOR), matching `ontolith.propose`'s existing validation. The
author is always resolved server-side from the bearer token, never
client-supplied (ADR-0014). `flag_contradiction` is excluded from this
slice — it belongs with `/contradictions` (list + resolve), deferred in
full to the follow-up PR rather than introduced piecemeal now.

**5. Response models are typed Pydantic classes, not raw dicts** (unlike
`mcp.py`'s tool functions) — a free improvement from using FastAPI: OpenAPI
schema generation comes for free from typed responses.

## Rationale

**Why not the full SPEC §14.3 surface in one PR:** it spans every capability
tier (read, propose, write, review, admin) — the single largest interface
surface in the project to date. Shipping read + propose first (mirroring
MCP's already-proven slice) de-risks the FastAPI/auth wiring itself before
layering the higher-stakes write and admin paths on top, and keeps this PR
independently reviewable.

**Why auth-on-reads now instead of matching MCP's current behavior:** REST
is network-reachable by nature — unlike the CLI/SDK's direct local process
access — so defaulting to the SPEC's stated posture is the safer choice for
a surface meant to be exposed over HTTP, even though it leaves MCP visibly
behind by comparison. Relaxing REST to match MCP's gap, instead of MCP being
raised to match REST's correct posture, was rejected as regressing a
correctly-scoped design to match a known bug in a different interface.

**Why not add `QueryBuilder.offset()` in this PR:** `QueryBuilder` only
supports `.limit()` today. Extending it is a `query/`+`store/` change, not
an `interfaces/`-only one, and is out of scope for "expose the existing SDK
over HTTP." Tracked in KI-022's deferred list.

## Alternatives Considered

**A REST-specific auth layer (session cookies / OAuth2 password flow),
rejected.** Would duplicate the token/principal model ADR-0014 already
built and proved via MCP, for a more conventional browser-facing pattern
that isn't needed yet — nothing in this slice is meant to be used from a
browser session directly.

**Matching MCP's current unauthenticated-reads behavior, rejected.** See
Rationale above — this was the closer call of the two, but SPEC §8.3/§17
are unambiguous that reads require capability-checking against a resolved
principal, and REST's read routes have no legacy behavior to preserve the
way MCP's shipped tools do.

## Consequences

**Positive:** No new auth infrastructure. Centralized error mapping. Free
OpenAPI docs from typed responses.

**Negative / follow-ups:** KI-021 (MCP read tools remained unauthenticated,
a gap this ADR surfaced but did not itself close) was resolved separately,
2026-07-22 — see ADR-0014's update note. `/query` has no pagination beyond
`.limit()` (KI-022). Direct write, proposal review, contradiction handling,
and principal/token admin were CLI/SDK-only until KI-022's follow-up PR,
which shipped the same day — see ADR-0022.

## References

- SPEC §14.3 (REST + GraphQL), §16 (error model), §8.3 (capabilities), §17
  (security model)
- ADR-0008 (MCP tool surface), ADR-0014 (MCP bearer-token authentication)
- `docs/known-issues.md` KI-021, KI-022
