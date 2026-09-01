# ADR-0042: Admin-Action Audit Trail — `AdminEvent` for create_principal/apply_schema/register_plugin, Direct Columns for Token Issuance/Revocation

**Status**: Accepted
**Date**: 2026-09-01
**Deciders**: Ontolith Core Team
**Related**: SPEC §17 ("all writes, proposal decisions, and resolutions are append-only and attributable"), ADR-0011 (accountable-owner DB-layer defense-in-depth), ADR-0014 (bearer-token authentication), ADR-0021 (REST interface — `GET /admin-events` route added by the KI-072 update), ADR-0022 (`create_principal` has no built-in capability check), ADR-0039 (retract's query-param precedent, reused for `actor`/`target`), ADR-0041 (audit-table immutability triggers — `admin_event` reuses the identical mechanism), KI-053/KI-054 (the admin-capability gaps this audit trail would have helped investigate), KI-060, KI-072

---

## Context

Before this ADR, the highest-stakes actions in the system — principal creation, schema application, plugin registration, API-token issuance and revocation — left no attributable trace at all. `Ontology.issue_token(principal_id, author)`/`revoke_token(credential_id, author)` both receive the acting admin's id and discard it; `PrincipalCredential` records only `principal_id`/`token_hash`/`created_at`/`revoked_at`. There was no event table for principal creation, schema application, or plugin registration either — the only audit tables that existed at all were `assertion_event`/`proposal_event` (SPEC §5/§9's own write/review trail), which don't cover admin-tier actions.

This mattered concretely, not just in the abstract: KI-053/KI-054 (the M3 milestone-boundary security audit) found real admin-capability gaps — a misconfigured AI principal that could mint tokens for humans, a CLI with no gate on `principal create` at all. Both are now fixed, but investigating *how far* either gap was actually exploited before the fix landed would have had no audit trail to work from.

## Decision

**Two different attribution mechanisms for two different shapes of action**, matching what each action's existing data model already supports:

**1. Token issuance/revocation — direct columns on `PrincipalCredential`.** `issued_by`/`revoked_by` (both `str | None`) added to the model and both backends' `principal_credential` table. `issue_token()` populates `issued_by=author` at construction; `revoke_token()` passes `author` through to `StorageBackend.revoke_credential(credential_id, revoked_at, revoked_by)` (a port signature change — **Breaking**). No separate event row: every credential already has a natural, permanent home for its own attribution, and a credential's issuance/revocation each happen at most once in its lifetime — there's no "history of attempts" to record the way there is for the other three actions.

**2. Principal creation, schema application, plugin registration — a new `AdminEvent`** (`identity/admin_event.py`): `id`, `actor`, `action` (`Literal["create_principal", "apply_schema", "register_plugin"]`), `target` (free text — a principal id, a `namespace:vN` schema descriptor, or a plugin name), `at`, optional `detail`. New `StorageBackend.put_admin_event()`/`get_admin_events(actor=, target=)` port methods. New `admin_event` table, backed by the identical three-trigger immutability mechanism ADR-0041 established for `assertion_event`/`proposal_event` (SQLite only — DuckDB has no `CREATE TRIGGER` support, same documented asymmetry). Wired in:
- `Ontology.apply_schema()`: records the event inside the same `with self.backend.transaction():` block as `put_schema()` — one atomic write, not two independent ones that could partially fail.
- `Ontology.create_principal()`: gained a new **optional** `author: str | None = None` parameter, used *only* to attribute the resulting event — **not** a capability gate. When `author is not None`, `put_principal()` and the event write share one transaction, same as `apply_schema`. `author=None` (the default) records nothing, covering the bootstrap case (a fresh database's first principal, with no admin yet to name).
- `PluginRegistry.register()`: records a `register_plugin` event via the new public `Ontology.record_admin_event()` method, only after registration actually succeeds (mirrors the KI-014 unenforced-capability warning's own "only on success" timing). `_ensure_principal()`'s own `create_principal(..., author=author)` call means a plugin's *first* registration also produces a `create_principal` event automatically — two events for one `register()` call in that case, one per action that actually happened.

REST's `POST /principals` route and the CLI's `principal create` command were both updated to pass their already-known admin id through as `author=` — they already call `require_admin`/gate on `--author` before invoking `create_principal` (ADR-0038, KI-054), so this is purely "also attribute what you already validated," not a new capability requirement.

## Rationale

**Why `create_principal` gained an optional `author` param instead of every external caller recording its own event separately** (the alternative this ADR's KI explicitly weighed): baking attribution into the method itself means a future caller can't forget to also log it — the same robustness argument ADR-0041 makes for schema-level triggers over "trust every caller to remember." The parameter is additive and keyword-only, so it doesn't reverse ADR-0022's actual decision (capability gating still happens entirely outside `create_principal`, exactly as before) — it only gives the method something optional to attribute *to*, the same shape `apply_schema`'s pre-existing `author` parameter already has.

**Why `target` has no foreign key, and why `admin_event.actor` doesn't either (unlike `assertion_event`/`proposal_event`'s `actor`, which do):** `target` would need to reference three different row types depending on `action` — no single column type can do that, so this follows `ProposalEvent.type`'s own established precedent (trusting Pydantic's `Literal` at construction, no DB-level CHECK/FK) rather than inventing a new pattern. `actor` specifically can't have a FK because `create_principal`'s `author` is optional and deliberately unvalidated (per the decision above) — a FK would reject a `create_principal` event whose caller supplied a bogus author string, when every other actor-recording code path in this codebase just records what it's given.

**Why token issuance/revocation don't get an `AdminEvent` too** (making all four actions uniform): a `PrincipalCredential` row already *is* a permanent, queryable record of exactly one issuance and at most one revocation — adding a parallel event table for the same two facts would duplicate data with no new information, unlike the other three actions, which had no existing row-level home for "who did this."

**Why re-revoking an already-revoked credential is now a true no-op, not a second overwrite** (found while implementing this, not originally part of the KI's own Fix text): the pre-existing `revoke_credential` docstring claimed "idempotent-safe: re-revoking is a no-op update," but functionally it just re-ran the same `UPDATE` unconditionally — harmless before `revoked_by` existed (nothing meaningful changed on a second call), but exactly the attribution-laundering bug this ADR exists to prevent once `revoked_by` did: a second admin calling `revoke_token` on an already-revoked credential would have silently overwritten who actually revoked it first. Fixed by adding `AND revoked_at IS NULL` to the `UPDATE`'s `WHERE` clause and distinguishing "already revoked" (silent no-op, first attribution stands) from "genuinely not found" (still raises `StorageError`, unchanged).

## Consequences

**Positive:**
- Closes KI-060: the four highest-stakes admin actions are now attributable, closing the exact gap that would have helped investigate KI-053/KI-054's blast radius had this existed sooner.
- `apply_schema`/`create_principal`'s event recording is transactional with the underlying write — no possible partial state.
- Reuses ADR-0041's immutability mechanism directly rather than inventing a fourth audit-table design — `admin_event` gets the identical SQLite guarantee (and identical documented DuckDB gap) with no new design surface.

**Negative / follow-ups:**
- **Breaking**: `StorageBackend.revoke_credential()` gained a required `revoked_by: str` parameter — any external `StorageBackend` implementation must update its signature.
- ~~No REST/GraphQL/CLI/MCP surface exposes `get_admin_events()` yet — this ADR is scoped to *recording* the trail (what KI-060's own Fix text asked for), not to querying it through any production interface. Left as explicit future scope, not silently dropped.~~ **Closed by KI-072 — see Update below.**
- `create_principal`'s `author` param is optional and unvalidated by the method itself — a caller that passes a nonexistent principal id gets an event recorded with that bogus actor, no error. This matches the method's own pre-existing "no built-in capability check" posture (ADR-0022) rather than introducing a new validation surface inconsistent with it.
- Plugin registration's "two events for a first-time registration, one for a re-registration" shape is a real asymmetry a consumer of `get_admin_events()` needs to know about — documented in `PluginRegistry.register()`'s own docstring, not hidden.
- **`create_principal`/`apply_schema` now open their own `self.backend.transaction()` (to keep the governed write and its `AdminEvent` atomic) — found in review to be a real, undocumented behavior change**: neither method previously opened a transaction of its own (they called `put_principal`/`put_schema` directly), so a caller could previously compose either inside its own `with kb.backend.transaction():` block; that now raises `StorageError` ("cannot start a transaction within a transaction"), reproduced independently in both backends. This is not a new *pattern* in this codebase — 11 of `Ontology`'s other write methods already require being called standalone, for the identical reason — but it is a real change for these two specific methods, and no test previously pinned either the old composability or the new constraint. Documented explicitly in both methods' own docstrings (`Note:` sections) rather than left implicit. Not fixed by making `StorageBackend.transaction()` reentrant — that would be a materially larger change to the transaction primitive itself, out of scope for this KI, and no other codebase caller currently needs it.
- **`Ontology.record_admin_event()` is public and does not itself capability-check `actor`** — any code holding an `Ontology` handle can write an arbitrary `(actor, action, target)` triple into the (now immutable) `admin_event` table, including attributing an action to a principal that never performed it. This is a deliberate, not accidental, consequence of the same design this ADR's "Alternatives Considered" section already argues for `actor` having no FOREIGN KEY: `create_principal`'s own `author` is optional and unvalidated by design (ADR-0022), so `record_admin_event` cannot re-gate `actor` without breaking that call site specifically. The method is public (not a leading-underscore helper, unlike `_record_assertion_event`) because `PluginRegistry` — a different module — needs to call it directly, the same way it already calls the public `require_admin`/`create_principal`. Every real call site (`apply_schema`, `create_principal`, `PluginRegistry.register`) already validates or intentionally leaves unvalidated its own `actor` before calling this; a hypothetical caller that skips that step and forges an event is the same trust boundary as a hypothetical caller that skips `require_admin` before any other admin-tier `Ontology` method. `src/ontolith/plugins/views.py`'s "structurally absent from ReadOnlyView/WriteView" list was updated to name `record_admin_event` explicitly alongside the other admin-only methods it already excludes.

## Alternatives Considered

- **Every external caller (REST, CLI, PluginRegistry) records its own event after calling `create_principal`, leaving the method's signature untouched**: rejected — see Rationale; this was the KI's own named alternative, judged less robust than baking attribution into the method as an optional parameter.
- **One unified event table/mechanism for all four actions, including token issuance/revocation**: rejected — see Rationale; `PrincipalCredential` already has a natural home for its own attribution, and duplicating it into a second table would be redundant, not more complete.
- **A FOREIGN KEY on `admin_event.actor` referencing `principal(id)`, matching `assertion_event`/`proposal_event`**: rejected — `create_principal`'s `author` is deliberately optional and unvalidated (ADR-0022), so a FK would reject a legitimate (if attacker-supplied) event write that every other part of this design treats as "record what you're given."

## Update (2026-09-01, closes KI-072): REST and CLI read surfaces for the audit trail

The gap this ADR's own Consequences named — `get_admin_events()` recorded but unreachable through
any interface — is closed for REST and the CLI (GraphQL/MCP left for their own future scope, per
KI-072's own Fix text: "extend to GraphQL/MCP/CLI as those surfaces need it").

**`Ontology.get_admin_events(author, *, actor=None, target=None)`** — new, admin-gated the same way
`list_tokens`/`list_principals` already are (`self.require_admin(author)` first, matching the
project's existing "record, don't re-gate on read" precedent for this admin-tier data: reading who
did an admin action is itself sensitive, so it gets the same floor as issuing/listing credentials,
not the lower bar ordinary read tools clear). Thin wrapper over the already-built
`StorageBackend.get_admin_events(actor=, target=)` — no new port method, no new domain logic.

**REST**: `GET /admin-events`, optional `actor`/`target` query parameters (mirrors
`GET /contradictions`'s `state` query-param precedent for a similarly small set of optional
filters, same reasoning ADR-0039 already used for `retract`'s `acting_as`). New `AdminEventOut`
response model. `CredentialOut` (`GET /principals/{id}/tokens`) gains `issued_by`/`revoked_by` —
`PrincipalCredential` has carried both fields since KI-060, but the REST response model never
surfaced them.

**CLI**: new `ontolith admin-events list [--actor] [--target] --author <id>` command (a new
top-level `admin-events` Typer sub-app, matching `namespace`/`schema`'s existing pattern of one
sub-app per noun) — thin wrapper around the same `Ontology.get_admin_events()`. `principal
list-tokens`'s existing output line extended to show `issued_by`/`revoked_by` alongside each
credential's id/timestamps/status.

**Why REST and CLI, not GraphQL/MCP too:** REST is explicitly named as "the natural first target"
in KI-072's own Fix text (matching how other read-only queries are exposed); CLI was added because
it directly serves this KI's own motivation (an operator investigating "who did this" is a CLI-first
workflow, and the marginal cost was one more thin wrapper over a method REST already needed). MCP
is deliberately not extended: SPEC's no-direct-write-tool rule doesn't block a new *read* tool, but
`get_admin_events()`'s admin-gating would make it the first MCP tool requiring `admin` capability
rather than `read`/`propose` (every existing MCP tool sits at one of those two tiers) — a genuine
new precedent, not a mechanical port of the REST route, and better decided if/when an actual MCP
consumer needs it rather than spun up speculatively here. GraphQL wasn't named as urgent by the KI
and has no existing precedent this change would directly extend (unlike REST's query-param pattern
and CLI's per-noun sub-app pattern, both already established elsewhere).
