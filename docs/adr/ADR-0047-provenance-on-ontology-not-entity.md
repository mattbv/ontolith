# ADR-0047: Provenance Is an `Ontology` Method, Not an `Entity` Method

**Status**: Accepted

**Date**: 2026-09-10

**Deciders**: Ontolith Core Team

**Related**: SPEC §5.4 (provenance is a derived view, retrievable "for any assertion in one
call"), SPEC §14.1 (Python SDK sketch — lists `Entity.history()`, `Entity.provenance(predicate)`,
`Entity.contradictions()` and a single `principal(...)` factory), ADR-0036 (`ReadOnlyView` — the
"safe method subset of `Ontology`" shape plugins get), `docs/known-issues.md` KI-086 (closed by
this ADR)

---

## Context

SPEC §5.4 requires provenance to be retrievable for any assertion **in one call**. That capability
existed only three times over, at the interface layer: REST's `provenance_route`, GraphQL's
`_build_provenance`, and MCP's `provenance_tool` each independently reassembled the same
`backend.get_assertion()` + `get_proposal_events()` + `get_assertion_events_by_successor()` logic
before shaping it into their own response type. There was no domain-layer method — `Ontology` had
no `provenance()` under any name, and `core/entity.py` is a flat frozen Pydantic value object with
no methods at all.

This "logic implemented once per interface" pattern is the exact shape this project's KI history
keeps re-finding (KI-058/059/075/076/077/079, among others): each cross-interface parity gap
needed a dedicated KI to close after the fact. KI-086 was filed to remove the risk class at the
source.

KI-086 also flagged two related drifts between SPEC §14.1's Python-SDK sketch and the shipped API:

1. §14.1 lists `Entity.history()`, `Entity.provenance(predicate)`, `Entity.contradictions()` as
   methods on `Entity`. None exist.
2. §14.1 lists a single `principal(id, *, kind, ...)` factory on the knowledge base. The SDK ships
   `create_principal()` and `get_principal()` as two separate methods; `kb.principal(...)` raises
   `AttributeError`.

Under SPEC §1's Conventions ("Code and DDL are *normative for shape*"), the §14.1 code block is
normative for shape — so `Entity.history()` etc. not existing, and `principal(...)` being two
methods, are **deviations from the spec's stated shape**, not just informal drift. §14.1 is headed
"(primary surface)", not "(normative shape)" like §6.2/§11.1/§12.2, but §1 is the governing
statement and it makes no such carve-out. This ADR is the record of those deviations and the
reasoning for them — the standard way this project documents a deliberate departure from SPEC
(cf. ADR-0041 for DuckDB trigger support, ADR-0013's KI-068 update). §14.1 has in fact diverged
from the implementation in ~half a dozen other places too, unrelated to KI-086 (`Ontology.connect`'s
signature, `KnowledgeBase` vs `Ontology`, `get(Concept, **natural_key)` vs `get_entity(id)`, the
`Principal.propose`/`can` methods) — this ADR scopes itself to the two KI-086 named and leaves the
rest to the SDK's own docstrings. The §14.1 sketch also predates the decision to make `Entity` a
pure value object (SPEC §5.2, `core/entity.py`).

## Decision

**1. Add `Ontology.provenance(assertion_id: str) -> Provenance`.** One domain-layer implementation
of SPEC §5.4's one-call view. Returns a new frozen `Provenance` value object
(`ontolith.govern.provenance`, exported from `ontolith.govern`) carrying:

- `assertion: Assertion` — the assertion itself, with all its existing provenance fields
- `review_events: tuple[ProposalEvent, ...]` — every review action on the originating proposal, in
  order; empty for a direct write
- `superseded_ids: tuple[str, ...]` — the full predecessor set superseded (KI-008)

REST/GraphQL/MCP each call `kb.provenance()` and shape its result into their own response DTO
(`ProvenanceOut` / `ProvenanceType` / a dict). The DTO shaping stays per-interface — that is
genuinely interface-specific (Pydantic vs Strawberry vs JSON) and is not duplicated *logic*, only
field copying. `NotFoundError` for an unknown id is now raised once, by `provenance()`, and each
interface's existing error mapping handles it unchanged.

**2. Provenance / history / contradictions stay `Ontology` methods; `Entity` stays a value
object.** `Entity` gets no backend or `Ontology` handle and no query methods. SPEC §14.1's
`Entity.history()` / `Entity.provenance(predicate)` / `Entity.contradictions()` sketch is **not**
implemented as written. The equivalent capabilities all exist on `Ontology` already or now:

| SPEC §14.1 sketch            | Shipped equivalent                                          |
|------------------------------|------------------------------------------------------------|
| `Entity.history()`           | `Ontology.assertions(subject=…, status=None)`               |
| `Entity.provenance(predicate)` | `Ontology.provenance(assertion_id)` (per assertion, not per predicate) |
| `Entity.contradictions()`    | `Ontology.contradictions(state=…)`                          |

**3. The `create_principal()` / `get_principal()` split is intentional.** They differ in
semantics (mint vs look up), in capability (creation is gated per ADR-0038/ADR-0022; lookup is
not), and in return contract (`create_principal` raises on conflict; `get_principal` returns
`None`). No `kb.principal(...)` alias is added — a single overloaded factory would blur those
boundaries.

**4. SPEC §14.1 is annotated, not rewritten.** A short note under the block points at this ADR as
the authoritative record where the shipped surface deviates, rather than editing the block
line-by-line for the two deviations KI-086 named while leaving the others. The note does not
relabel §14.1 as non-normative — §1's Conventions still govern; it flags the deviations as
ADR-recorded.

## Rationale

- **`Entity` as a value object is load-bearing.** It is passed around freely, cached, compared by
  value, and serialized at every interface boundary. Giving it a live `Ontology`/backend handle to
  satisfy `entity.history()` would make it stateful, un-pickleable in the obvious way, and a
  lifetime-management hazard (an `Entity` outliving its `Ontology`). SPEC §5.2's "entities carry no
  attribute values directly" is the same principle; §14.1's method sketch simply predates it.
- **`Ontology.provenance()` is the smallest change that removes the duplication.** The assembly is
  ~10 lines; putting it behind one method and calling it three times is strictly less code than
  the status quo, with no new abstraction.
- **`Provenance` lives in `govern/`, not `core/`.** It references `ProposalEvent` (a `govern`
  type); `core/` importing `govern/` would invert the dependency direction. `govern/` is where
  proposals, contradictions, and now this derived view live.
- **Consistency with `ReadOnlyView` (ADR-0036).** Plugins get a "safe method subset of `Ontology`"
  — a domain-layer `provenance()` is a method that subset can expose later without another
  interface-layer reimplementation.

## Consequences

**Positive:**
- One implementation of SPEC §5.4's one-call view; the next provenance change touches one place.
- `Provenance` is a typed, importable SDK return value — direct SDK callers get provenance without
  going through an interface.
- The `Entity`-methods and `kb.principal` questions are settled with a written rationale instead of
  staying undocumented drift.

**Negative:**
- `Provenance` joins the public API surface (`ontolith.govern`), so it is now covered by ADR-0019's
  stability policy and `test_public_api_surface.py`.
- SPEC §14.1 still doesn't match the code field-for-field; readers must follow the note to this
  ADR. Accepted as better than a piecemeal rewrite of a sketch.
- `Entity.history()`-style ergonomics (`entity.history()` reads better than
  `kb.assertions(subject=entity.id, status=None)`) are not delivered. A future ADR could add them
  as free functions or a thin non-stateful accessor if demand appears; this ADR does not.

## Alternatives Considered

**Give `Entity` an `Ontology` handle and implement §14.1 literally.** Rejected — reverses the
value-object decision for an ergonomic gain, with real lifetime/serialization costs (see
Rationale).

**Put the assembly in a `core/provenance.py` free function instead of an `Ontology` method.**
Viable, but `Ontology` is already the domain entry point every interface holds, and a method keeps
it discoverable next to `assertions()` / `contradictions()`. A free function would also still need
`core/` to reach `govern.ProposalEvent`.

**Rewrite SPEC §14.1 to match the implementation.** Out of proportion — §14.1 is a sketch with
many divergences; fixing two of them line-by-line implies the rest are accurate. The note-plus-ADR
approach records the whole surface once.

**Add a `kb.principal(...)` alias.** Rejected — collapses create-vs-get, which differ in
capability gating and error contract.
