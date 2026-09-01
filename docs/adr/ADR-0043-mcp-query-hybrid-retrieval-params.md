# ADR-0043: MCP's `ontolith.query` Gains REST/GraphQL's Hybrid-Retrieval Params, Plus `as_of`

**Status**: Accepted
**Date**: 2026-09-01
**Deciders**: Ontolith Core Team
**Related**: SPEC §11.3 (hybrid retrieval), §11.4 (bitemporal `as_of`), §14.4 (MCP server — normative tool table names `semantic`, `as_of`, `min_confidence` explicitly), ADR-0008 (MCP tool surface), KI-018 (hybrid retrieval), KI-037/KI-047 (`min_confidence`/`trust_at_least`), KI-058

---

## Context

`QueryBuilder` gained `.semantic()`, `.min_confidence()`, `.trust_at_least()`, and `.limit()` across M3 (KI-018, KI-037, KI-047), and REST's `POST /query`/GraphQL's `Query.query` both wire all four straight into it. `interfaces/mcp.py`'s `query_tool` was never updated to match — it accepted only `concept`/`token`/`filters`/`namespace`, leaving MCP's own audience (AI agents) with no way to do semantic retrieval, confidence/trust filtering, or even cap result size, despite SPEC §14.4's own normative tool table naming `semantic`, `as_of`, and `min_confidence` for this exact tool. KI-018's hybrid-retrieval PR didn't touch MCP; the M2 MCP PR predates hybrid retrieval entirely — the gap was invisible to any single PR's own review and only surfaced comparing all three interfaces side by side at the M3 milestone boundary.

While wiring this, two adjacent things needed a decision rather than a silent carry-forward:

1. **`as_of` isn't wired into REST or GraphQL's query routes at all** — neither exposes bitemporal time-travel on `/query`/`Query.query` today (`kb.as_of(t)` is SDK-only). SPEC §14.4 names `as_of` for MCP's tool specifically, though, and KI-058's own Fix text asks for it — so this ADR adds it to MCP alone, making MCP the first of the four shipped interfaces to expose bitemporal time-travel through any route at all.
2. **`query_tool`'s existing `namespace: str = "default"` parameter was already dead code.** `Ontology.query(concept)` has no `namespace` argument — `Ontology.namespace` is hardcoded to `"default"` (the same M1 limitation REST's own `QueryIn` docstring already documents, which is why REST never added a `namespace` field to its request body, and why GraphQL's resolver has none either). MCP's parameter was accepted and silently ignored on every call.

## Decision

**Add `semantic`, `as_of`, `min_confidence`, `trust_at_least`, `limit` to `query_tool`, mirroring REST/GraphQL's `QueryBuilder` wiring exactly for the first four. Remove the dead `namespace` parameter, matching REST/GraphQL's own convention of not exposing one.**

```python
def query_tool(
    concept: str,
    token: str | None = None,
    filters: dict[str, str] | None = None,
    semantic: str | None = None,
    as_of: str | None = None,
    min_confidence: float | None = None,
    trust_at_least: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
```

`as_of` (an ISO-8601 string, matching `Ontology.as_of(t: datetime | str)`'s own accepted shape) is applied first — `builder = kb.as_of(as_of).query(concept)` instead of `kb.query(concept)` — since `AsOfView.query()` returns the same `QueryBuilder` type with `as_of_time` already set, so every other parameter chains onto it identically whether or not `as_of` was given. A malformed value (`datetime.fromisoformat` raising `ValueError`) returns `{"error": ..., "code": "validation_error"}`, matching the tool's existing error shape for a bad `filters` key.

## Rationale

**Why `as_of` is added to MCP even though REST/GraphQL don't have it:** SPEC §14.4's normative tool table is specific to MCP and names it explicitly; KI-058's Fix text asks for it by name. Waiting for REST/GraphQL to gain their own `as_of` support first would mean continuing to under-implement a named SPEC requirement for MCP's own tool table. Adding it to REST/GraphQL's query routes is out of scope here — not asked for by this KI, and a separate design question (REST's `QueryIn` docstring already explains why it has no `namespace` field for a related M1-limitation reason; a symmetric `as_of` addition there is its own decision, not a mechanical port of this one).

**Why the dead `namespace` parameter is removed, not left in place:** leaving it would mean this ADR's own rewritten docstring either documents a lie (claims it does something) or documents the bug (which is worse than just fixing it, given it's a one-line removal directly adjacent to the rest of this change) — and it was never functional to begin with, so no caller was relying on real behavior from it. REST/GraphQL already establish the convention of omitting a namespace field entirely for the same underlying reason; MCP now matches.

**Why `min_confidence`/`trust_at_least`/`limit` get no extra validation MCP doesn't already do elsewhere:** `QueryBuilder` itself performs no range checking on these (an out-of-range `min_confidence` simply matches nothing, not an error) — REST/GraphQL pass them through unvalidated too. Adding MCP-specific validation would introduce an inconsistency across interfaces, not close one.

## Consequences

**Positive:**
- Closes KI-058 — MCP's own audience (AI agents) can now do semantic/hybrid retrieval, confidence/trust filtering, and result-size capping, matching REST/GraphQL parity for these four parameters.
- MCP is now the only interface to expose `as_of`/bitemporal reconstruction at all, closing a SPEC §14.4 conformance gap none of REST/GraphQL/CLI happen to share.
- No new domain logic, no new `StorageBackend` port method — `query_tool` is a thin wrapper over the already-built `QueryBuilder`/`AsOfView`, identical in shape to REST's `query_route`.

**Negative / follow-ups:**
- **Breaking** for any MCP caller passing `namespace=` explicitly to `ontolith.query`: the parameter is removed, not deprecated. Judged low-risk since it was already a silent no-op — no caller could have been relying on real namespace-scoping behavior from it — but it is a signature change to a tool argument. MCP tool-schema stability has no formal ADR-0019-style policy yet (ADR-0019 explicitly excludes `interfaces/mcp` from its scope, calling this "a gap, not a decision"), so this isn't a violation of any stated guarantee, but is called out here and in the CHANGELOG regardless.
- REST and GraphQL's `/query` routes still have no `as_of` support — an agent using MCP can now time-travel a query; a REST/GraphQL client still cannot. Not addressed by this ADR; a candidate follow-up KI if parity across interfaces becomes a priority, but not filed speculatively here since no gap was found that names it as expected soon.
- KI-059 (MCP error-code drift from REST/GraphQL's taxonomy) is unaffected — this tool's new `validation_error` paths (malformed `as_of`, no-Embedder `semantic`) reuse the same drifted `"validation_error"` string literal the tool's `filters` handling already used, not `exc.code`.

## Alternatives Considered

- **Leave `namespace` in place, documented as a no-op**: rejected — a parameter that looks configurable but silently does nothing is worse for callers than one that doesn't exist; removing it is a one-line fix directly adjacent to the rest of this change, not a separate undertaking.
- **Defer `as_of` until REST/GraphQL gain it too, for interface parity**: rejected — SPEC §14.4 names `as_of` for MCP specifically and KI-058's own Fix text asks for it; withholding a named requirement from one interface because two unrelated interfaces don't have it yet doesn't serve conformance, it just spreads the gap out longer.
