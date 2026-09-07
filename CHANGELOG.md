# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### M3 - Extensible (0.3)

#### Added
- GraphQL and CLI parity for `Proposal.reviewers`/`assign_reviewers()` (closes KI-079, ADR-0046
  Update — see the KI-078 entry below for the feature this closes the interface gap on):
  `ProposalType` gains `reviewers: list[str]`; new `Mutation.assignReviewers(proposalId,
  reviewers)` (GraphQL's ninth mutation, structurally identical to `requestChanges`/
  `rejectProposal`). CLI gains `proposal assign <proposal_id> --actor <id> [--reviewer <id> ...]
  [--clear]` — `--reviewer`/`--clear` are mutually exclusive-by-requirement, since clearing is
  irreversible and the CLI has no other natural "the caller meant to clear everything" signal the
  way REST/GraphQL's explicit empty-list argument does; `proposal list` gains a
  `reviewers=<comma-joined>` suffix when non-empty (mirrors KI-075's `rationale_entries=<n>`
  convention). MCP remains deliberately excluded — `assign_reviewers()` would be its first
  review-capability write tool, left for a real consumer to motivate.
- `Proposal.reviewers: list[str]` and `Ontology.assign_reviewers(proposal_id, reviewers, actor)`,
  implementing SPEC §9.4's `assign` review action (closes KI-078, ADR-0046 — GraphQL/CLI parity
  closed separately above, KI-079). A `PolicyStrategy`'s `RequireReview.reviewers` was computed by
  every strategy but never persisted or surfaced anywhere — now populated on `Proposal` at creation
  time and refreshed on `resubmit`'s re-evaluation (KI-027), not cleared on accept/reject/
  request_changes. `assign_reviewers()` replaces the reviewer list wholesale, records a
  `ProposalEvent(type="assign")`, and reuses the exact eligibility checks accept/reject/
  request_changes already share (review/admin capability, non-AI, no self-review — kept for
  consistency with the sibling actions, though not currently load-bearing since `reviewers` itself
  isn't enforced at accept time; see ADR-0046). Exposed via REST only at first: `POST
  /proposals/{proposal_id}/assign`, and `reviewers` added to `ProposalOut` on every proposal
  route. **Breaking:** `StorageBackend` gains a required `update_proposal_reviewers()` method; both
  backends migrate existing database files to add the new column — DuckDB's `ALTER TABLE ADD
  COLUMN` rejects any
  constraint (`NOT NULL`, `UNIQUE`, `CHECK` all fail identically), but a plain `DEFAULT` isn't
  itself a constraint, so its migrated column uses `DEFAULT '[]'` instead, which DuckDB backfills
  into existing rows automatically, unlike a fresh database's stronger `NOT NULL DEFAULT '[]'`. A
  manual `assign_reviewers` call also doesn't survive a later `resubmit` that lands back in
  `require_review` (that branch's policy re-evaluation overwrites `reviewers`) — a `resubmit` that
  instead auto-accepts or gets rejected leaves a manual assignment untouched. Documented and pinned
  by tests covering all three resubmit outcomes.
- Three new `PolicyStrategy` implementations closing out SPEC §9.2's built-in strategy list (closes
  KI-069, ADR-0045): `ConfidenceThreshold(threshold, reviewers=None)` auto-accepts once a
  proposal's own staged confidence meets `threshold` (a missing confidence always requires review,
  never assumed as 0 or 1); `SourceRequired(reviewers=None)` auto-accepts only when the operation
  carries a non-empty `source` (no `kb` read — a narrower, unconditional cousin of `SourceQuorum`,
  composable with it via `Composite` for "sourced AND quorum'd"); `RequireReviewByRole(
  role_reviewers, *, default=None)` never auto-accepts — it always routes to review, choosing
  reviewers by `principal.metadata.get("role")` (no dedicated `Principal.role` field exists;
  `metadata` is the existing documented extension point), read from the real author, never
  `acting_as`, so delegation can't be used to dodge a role's reviewers. All three enforce the
  KI-015 capability floor themselves, mirroring `SourceQuorum`, and are exported from
  `ontolith.govern`. SPEC §9.2's six-strategy SHOULD-list is now fully built (`ThresholdPolicy`
  continues to cover roughly what `TrustLevel` would; no class of that name exists separately).
  Also fixed along the way: `RequireReview.__init__` now copies its `reviewers` argument instead
  of aliasing it — a caller mutating a returned decision's `.reviewers` previously rewrote the
  issuing strategy's own configuration for good, a latent bug in every pre-existing strategy too.
  Filed KI-078 during review: nothing in the system persists or surfaces a `RequireReview`'s
  `reviewers` anywhere yet — pre-existing, but `RequireReviewByRole`'s entire purpose being
  reviewer routing makes it far more consequential now.
- New MCP tool `ontolith.list_contradictions` (closes KI-076): `read`-tier, mirroring REST's
  `GET /contradictions`/GraphQL's `Query.contradictions` — `ontolith.flag_contradiction`
  (propose-tier, mutates) was previously the only MCP surface that returned a contradiction at
  all, so reading one back (including its `rationale_history`, KI-075) required a write. `state:
  str | None = "open"` accepts `"open"`/`"resolved"`, `"all"` or `None` for every state (both work
  identically — `"all"` kept for consistency with REST/GraphQL's own sentinel, even though MCP's
  JSON `null` doesn't share the HTTP-query-string ambiguity that sentinel exists to work around),
  or a `validation_error` for anything else (review finding: an unrecognized value previously
  matched zero rows silently, indistinguishable from "no contradictions exist"). Returns
  `rationale_history` via the same `govern.contradiction.safe_rationale_history()` helper
  `ontolith.flag_contradiction`'s response was also switched to in this fix (review finding: the
  two tools previously guaranteed different shapes for the same field on the same contradiction —
  a malformed `metadata` blob degraded to `[]` on one and passed through raw, unprojected garbage
  on the other). MCP now has 9 tools (was 8 as of KI-067).
- Read surface for a contradiction's accumulated `rationale_history` (closes KI-075): REST's
  `ContradictionOut` gains a `metadata: dict[str, Any]` field (all three routes that return one —
  `GET /contradictions`, `POST /contradictions/flag`, `POST /contradictions/{id}/resolve`).
  GraphQL's `ContradictionType` gains `rationaleHistory: [RationaleEntryType!]!` instead — GraphQL
  has no native map scalar, so the trail is projected into a structured `{rationale, actor, at}`
  type rather than exposed as an opaque blob (same reason `FilterInput` already exists as an
  explicit key/value list). MCP's `ontolith.flag_contradiction` response gains a
  `rationale_history` key carrying the *full* trail, not just the value passed to that call —
  still the only MCP surface that returns a contradiction at all (KI-076, filed during review: no
  read-only `list_contradictions`-shaped tool exists). CLI's `contradiction list` output gains a
  `rationale_entries=<n>` suffix (omitted when a contradiction has no history) plus a
  `--show-rationale` flag that prints every entry's full text (mirroring `flag`'s own format —
  added during review, since a bare count with no way to read the text on a pure read path
  defeated the point; named distinctly from `flag`'s own `--rationale <text>` option to avoid a
  same-flag-different-meaning trap between the two commands), and `contradiction flag` echoes
  every accumulated entry on its own line after the summary. Mirrors KI-072's own shape: the data
  was captured (KI-071) but unreachable through any interface but the raw SDK. `contradiction
  resolve` (CLI) has no equivalent flag — `resolve_contradiction()` takes no `rationale` input,
  and a resolved contradiction's trail is reachable via `contradiction list --state resolved
  --show-rationale`. New `govern.contradiction.safe_rationale_history()` — used by GraphQL's and
  the CLI's entry-rendering instead of per-field `.get(key, default)` (review finding: `.get()`
  alone still raised on a non-dict entry or a non-list `rationale_history`, and silently passed a
  present-but-`None` field value through instead of defaulting it, since the key check `.get()`
  performs doesn't cover either case) — `metadata`/`rationale_history` is an open, schema-less
  blob (ADR-0041), and a malformed or legacy-shape entry previously raised an uncaught exception —
  on GraphQL, one that failed the entire `contradictions` query, not just the one bad
  contradiction.
- Read surface for the admin-action audit trail (closes KI-072, ADR-0042 update): new
  `Ontology.get_admin_events(author, *, actor=None, target=None)`, admin-gated the same way
  `list_tokens`/`list_principals` already are. REST gets `GET /admin-events` (`actor`/`target`
  query filters, new `AdminEventOut` model) and `CredentialOut` gains `issued_by`/`revoked_by`. CLI
  gets `ontolith admin-event list [--actor] [--target] --author <id>`, and `principal
  list-tokens`'s output now shows who issued/revoked each credential. GraphQL/MCP left for their
  own future scope — MCP specifically, since admin-gating this would make it the first MCP tool
  requiring `admin` capability rather than `read`/`propose`.
- MCP's `ontolith.query` tool gains `semantic`/`as_of`/`min_confidence`/`trust_at_least`/`limit`
  parameters (closes KI-058, ADR-0043), mirroring REST's `POST /query`/GraphQL's `Query.query`
  wiring into `QueryBuilder` for the first three and `limit`; `as_of` is new even to REST/GraphQL,
  making MCP the first of the four shipped interfaces to expose bitemporal time-travel (SPEC
  §11.4) through any route, per SPEC §14.4's own normative tool table naming it for this tool
  specifically. Also removed the tool's pre-existing `namespace` parameter, a silent no-op
  (`Ontology.query()` never accepted a namespace argument; namespace is hardcoded, the same M1
  limitation REST/GraphQL already work around by never exposing the field) — schema-visible, not
  runtime-breaking: FastMCP drops unrecognized tool arguments rather than rejecting the call, so a
  caller still passing `namespace=` keeps succeeding exactly as it silently did before.
- Admin-action audit trail (closes KI-060, ADR-0042): `PrincipalCredential` gains `issued_by`/
  `revoked_by` columns (both backends, migrated in place for existing database files), populated
  from `issue_token`/`revoke_token`'s already-required `author` parameter. New `AdminEvent`
  (`identity/admin_event.py`) and `admin_event` table record `create_principal`/`apply_schema`/
  `PluginRegistry.register` — reuses the exact SQLite-trigger immutability mechanism KI-066/
  ADR-0041 built for `assertion_event`/`proposal_event` (DuckDB has the same documented gap).
  `Ontology.create_principal` gained an optional `author` parameter, used only to attribute the
  resulting event — not a new capability gate; `ADR-0022`'s "no built-in check" decision is
  unchanged. REST's `POST /principals` and the CLI's `principal create` both pass through the
  admin id they already validate. **Breaking:** `StorageBackend.revoke_credential()` gained a
  required `revoked_by: str` parameter — any external `StorageBackend` implementation must update
  its signature. Also fixed along the way: re-revoking an already-revoked credential was silently
  overwriting `revoked_by` on a second call (attribution laundering) — now a true no-op.
  **Behavior change:** `apply_schema`/`create_principal` now open their own transaction (to keep
  the governed write and its `AdminEvent` atomic), so calling either from inside a caller's own
  `with kb.backend.transaction():` block now raises `StorageError` instead of composing — matches
  the constraint 11 other `Ontology` write methods already had.
- CLI `ontolith contradiction flag <id_a> <id_b> --author <id> [--rationale <text>]` and
  `ontolith contradiction resolve <id> --winner <assertion_id> --reviewer <id>` (closes KI-063) —
  the CLI was the only one of the four shipped interfaces with no contradiction write surface at
  all (REST/GraphQL/MCP all had at least `flag`; REST/GraphQL also had `resolve` — MCP
  deliberately doesn't, per ADR-0008/KI-009's reviewer-only scoping). Both are thin
  wrappers around `Ontology.flag_contradiction()`/`resolve_contradiction()`, mirroring existing
  CLI conventions (`flag`'s `--author` matches `assert`/`retract`; `resolve`'s `--reviewer`/
  `--author` alias matches `proposal accept`).
- `Composite(all=…, any=…)` policy strategy (`govern/policy.py`, SPEC §9.2, closes KI-061,
  ADR-0040) — the last of SPEC §9.2's six named strategies still missing. Combines multiple
  `PolicyStrategy` instances by decision severity (`Reject` > `RequireReview` > `AutoAccept`):
  every strategy in `all` must independently `AutoAccept` for the group to approve (the most
  restrictive decision wins); at least one strategy in `any` must (the least restrictive wins);
  same-severity decisions at the winning level are merged — `RequireReview.reviewers` as a
  dedup'd union, every `Decision.reason` concatenated — rather than one being silently discarded.
  Closes a real configuration gap: `SourceQuorum` deliberately does not special-case AI-authored
  proposals the way the default `ThresholdPolicy` does (ADR-0025 §5, unchanged by this), so a
  deployment on `SourceQuorum` alone silently drops ADR-0003's "AI principals always require
  review" guarantee — `Composite` is SPEC §9.2's sanctioned way to layer that rule back on top,
  and until now it couldn't actually be built. No new "AI-always-reviews" strategy ships
  alongside it (`ThresholdPolicy` can't be reused for this without re-imposing its own capability
  gate); the five-line pattern is documented as an inline example in `Composite`'s own docstring.
  MCP's `ontolith.propose`/`ontolith.resubmit` tool docstrings, which previously stated the
  AI-review guarantee unconditionally, now correctly attribute it to the *default*
  `ThresholdPolicy` specifically.
- RDF/OWL bridge, export only (SPEC §13.3, ADR-0036): `schema.rdf.to_owl(schema)`
  translates a `SchemaIR` into an OWL ontology (`rdflib.Graph`) — concepts become
  `owl:Class`, properties become `owl:DatatypeProperty` (XSD-typed range), relations
  become `owl:ObjectProperty` (`owl:inverseOf` if declared). `cardinality="single"` is
  additionally typed `owl:FunctionalProperty` only when `temporality="static"` too — a
  `time_varying` predicate can hold multiple simultaneously-active assertions with
  non-overlapping validity windows (SPEC §10.2) even at `single` cardinality, and
  declaring it functional unconditionally would assert a real OWL inconsistency for
  that valid state. New `RdfExporter` reference plugin
  (`plugins.reference.rdf_exporter`, entry point `rdf-owl-exporter`) adds active
  assertions as RDF instance data on top — one `rdf:type` triple per distinct entity
  seen, one property triple per active assertion — and serializes the combined graph
  (Turtle by default; any `rdflib` format). Every property/relation IRI either module
  references is declared its own `owl:DatatypeProperty`/`owl:ObjectProperty` type,
  including an `owl:inverseOf` target the schema doesn't otherwise mention and a
  predicate a later schema version removed but an already-active assertion still uses
  (OWL 2 DL requires a declaration for every property IRI in use). New `rdflib`
  dependency in the `interop` extra, not the real `linkml`/`linkml-runtime` packages
  ADR-0013 already rejected for the adjacent YAML bridge. Deterministic,
  percent-encoded `urn:ontolith:{namespace}:...` IRI scheme (percent-encoding closes a
  real serialization crash on realistic LinkML-imported schema names — spaces, URL
  namespaces, non-ASCII — found in review), no dependency on a schema's LinkML-sourced
  `default_prefix`/`prefixes` metadata. `from_owl` (import direction) is explicitly out
  of scope for v1, and so is any representation of valid time, confidence, or
  provenance — every currently-active assertion becomes exactly one triple with none of
  that context, unlike `JsonExporter`. `Ontology` and `ReadOnlyView` both gain a new
  `schema()` method — the first reference plugin needing schema access, not just
  entity/assertion data.
- GraphQL interface, closing M3's last unstarted scope item (SPEC §14.3,
  ADR-0037): `create_graphql_app(kb, auth_provider)` (`interfaces/graphql.py`)
  serves a `strawberry`-backed schema at `/graphql` exposing `Entity`, `Assertion`,
  `Proposal`, `Contradiction`, and `Principal` types with `query`, `propose`, and
  `review` operations — SPEC's literal wording, deliberately narrower than REST's
  own extended write/admin surface (ADR-0022): no direct-write mutation, no
  principal creation or token issuance. `Query`: `schema`, `entity` (with a
  lazily-resolved nested `assertions` field), `query`, `provenance`, `proposals`,
  `contradictions`, `principals` (admin-gated). `Mutation`: `propose`,
  `acceptProposal`, `rejectProposal`, `requestChanges`, `resubmitProposal`,
  `flagContradiction`, `resolveContradiction`. Reuses ADR-0014 bearer-token auth,
  resolved once per request into GraphQL context (never raised there, so
  introspection stays reachable unauthenticated like REST's `/docs`) and checked
  per-resolver. A custom `strawberry.Schema.process_errors` override centralizes
  `OntolithError` → `extensions={code, detail}` mapping — the GraphQL analog of
  REST's single `OntolithError` exception handler — redacting `StorageError`/
  `PluginError` messages the same way REST does; any other resolver exception
  (not a domain error) is redacted identically with a new `extensions.code =
  "INTERNAL_ERROR"`, matching REST's generic, code-less 500 for the same
  failure class rather than leaking the raw message. `create_graphql_app`
  also gained `introspection` (default `True`; set `False` to disable
  `__schema`/`__type` independent of the `graphql_ide` toggle) and
  `docs_url`/`redoc_url`/`openapi_url` passthrough, matching
  `create_rest_app`'s existing parameters. `graphql` extra widened to
  `strawberry-graphql[fastapi]` plus `uvicorn` so it's installable standalone,
  without also needing `[rest]`.
- Class-based schema DSL compiler and `Ontology.apply_schema` (SPEC §6.2)
- LinkML-aligned YAML schema front-end: `to_yaml`/`from_yaml`, a deliberately-scoped
  dialect subset documented in ADR-0013, with schema-level `default_range` support and
  a fail-loud policy on unsupported LinkML constructs (`abstract`, `identifier`, `key`,
  `alias`, `ifabsent`, `readonly`, `recommended`, and others) rather than silently
  dropping them
- `Ontology.proposals(state=)`/`contradictions(state=)` — reviewer-queue listing, backed
  by new `StorageBackend.proposals()`/`contradictions()` port methods; CLI commands
  `ontolith proposal list`/`ontolith contradiction list` (`--state`, `--all`)
- Hybrid retrieval (SPEC §11.3/§12.3/§14, ADR-0020, closes KI-018): `Embedder` port
  (`ontolith.core.embedder`) with a dependency-free default (`HashingEmbedder`) and a
  deterministic test double (`LookupEmbedder`); `StorageBackend.vector_upsert`/
  `vector_search` on both SQLite (via `sqlite-vec`, now a required dependency) and DuckDB
  (via native `list_distance`); `QueryBuilder.semantic(text)`, `.min_confidence(t)`,
  `.trust_at_least(l)`, `.limit(n)`; `Ontology.reindex(concept=)` to explicitly (re-)embed
  entities into the vector index; CLI `ontolith reindex [--concept]`
- REST interface, read + propose slice (SPEC §14.3, ADR-0021, partially closes KI-022):
  `create_rest_app(kb, auth_provider)` (`interfaces/rest.py`) exposing `GET /schema`,
  `GET /entities/{id}`, `POST /query`, `GET /provenance/{id}`, `POST /proposals`,
  `GET /proposals` — every route, reads included, requires an ADR-0014 bearer token (at
  the time, a deliberate divergence from MCP's then-unauthenticated read tools, tracked
  as KI-021 and since resolved — see below). One `OntolithError` → HTTP status handler
  (SPEC §16) replaces per-route error handling.
- REST interface, write/review/admin slice (SPEC §14.3, ADR-0022, further closes
  KI-022): `POST /assertions` (direct write via `assert_literal`/`assert_ref`),
  `POST /proposals/{id}/accept|reject`, `GET /contradictions`,
  `POST /contradictions/flag`, `POST /contradictions/{id}/resolve`, `POST /principals`,
  and `/principals/{id}/tokens` (`POST` issue, `GET` list, `DELETE` revoke) — reusing
  ADR-0021's auth and error-mapping unchanged. Eight of ten routes need no new
  capability-check code (the wrapped `Ontology` methods already gate themselves); the
  ninth, `GET /contradictions`, needs none either but only because it's a read;
  `POST /principals` calls `Ontology.require_admin()` explicitly, since
  `create_principal` has no built-in gate of its own. `GET /principals` (list),
  `GET /namespaces`, and `/proposals/{id}/review` remain deferred — no backing SDK
  method exists for any of the three (confirmed by an explicit audit, not an oversight).
  `DELETE /principals/{id}/tokens/{credential_id}` verifies the credential actually
  belongs to `principal_id` before revoking, raising `NotFoundError` on mismatch.
- `GET /principals` (SPEC §14.3, ADR-0022 update, further closes KI-022): new
  `Ontology.list_principals(author)`, gated the same way as `issue_token`/
  `revoke_token`/`list_tokens` (`require_admin`); REST route requires admin
  capability; CLI `ontolith principal list`. No pagination, matching `list_tokens`'s
  existing precedent.
- **Breaking:** `StorageBackend` gained a new required Protocol method,
  `list_principals() -> list[Principal]` (ADR-0022 update, closes KI-022's
  `GET /principals` gap) — any third-party `StorageBackend` implementation must add
  it.
- Full predecessor recovery for multi-target supersession (SPEC §10.2, ADR-0023, closes
  KI-008): `AssertionEvent.successor_id: str | None` records which successor caused a
  `"superseded"` event. `Assertion.supersedes` itself is unchanged (still scalar, still
  names only the first predecessor per SPEC §12.2's normative schema) — the full set of
  predecessors superseded by one incoming assertion is now recoverable via
  `{e.assertion_id for e in kb.backend.get_assertion_events_by_successor(successor_id)}`,
  surfaced as `ProvenanceOut.superseded_ids` (REST `GET /provenance/{id}`) and a matching
  `superseded_ids` key on the MCP `ontolith.provenance` tool.
- **Breaking:** `StorageBackend` gained a new required Protocol method,
  `get_assertion_events_by_successor(successor_id) -> list[AssertionEvent]` (ADR-0023,
  closes KI-008) — any third-party `StorageBackend` implementation must add it.
- **Breaking:** `StorageBackend` gained a new required Protocol method,
  `get_schema_at(namespace, at) -> SchemaIR | None` (SPEC §11.4, ADR-0024, closes
  KI-019) — any third-party `StorageBackend` implementation must add it. New
  `AsOfView.schema()` resolves the schema version effective at that view's `as_of`
  time (via `applied_at`, already recorded deterministically by every `put_schema`
  call) rather than always the latest version — `kb.as_of(t).schema()` now correctly
  differs across a schema migration boundary. `SchemaIR` and `put_schema` are
  unchanged.
- `SourceQuorum` policy strategy (SPEC §9.2, ADR-0025, closes KI-017):
  `PolicyStrategy.evaluate()` now sees KB state via a new `kb` parameter, pinned to a
  bitemporal `AsOfView` snapshot at the proposal's creation time. `SourceQuorum(threshold,
  reviewers=)` auto-accepts once `threshold` distinct sources corroborate the same
  `(subject, predicate, value)`, counting the proposal's own source together with
  matching, sourced, `kb`-visible assertions; retractions and sourceless proposals
  always require review; principals below `propose` capability are rejected (KI-015
  update). Does **not** reimplement `ThresholdPolicy`'s "AI proposals always require
  review" rule (ADR-0003) — an AI-authored proposal auto-accepts under `SourceQuorum`
  once quorum is reached; combining that guarantee with source-quorum is `Composite`'s
  job (still unbuilt).
- **Breaking:** `PolicyStrategy.evaluate()` gained a required `kb: KbView` parameter
  (SPEC §9.2, ADR-0025, closes KI-017), inserted between `principal` and `acting_as` —
  any third-party `PolicyStrategy` implementation must add it. `KbView` (new,
  `ontolith.govern.policy`) is a minimal structural Protocol (`assertions(subject=,
  predicate=) -> list[Assertion]`), not SPEC's literal `ReadOnlyView` — see ADR-0025 for
  why. `ThresholdPolicy` is unaffected at call sites: its own concrete signature keeps
  `kb` optional (unused), so existing callers that don't pass one are unchanged.
- `/proposals/{id}/review` (SPEC §9.1/§9.4, ADR-0022 update, further closes KI-022):
  new `Ontology.request_changes(proposal_id, reviewer, reason="")` — the third
  `under_review` outcome (`changes_requested`) alongside `accept_proposal`/
  `reject_proposal`, gated identically (review/admin capability, no AI reviewer, no
  self-review — see ADR-0022 for why this one keeps the strict gate too). Same
  reviewer-eligibility/proposal-state checks as its siblings, now factored into a
  shared `Ontology._require_reviewer` helper (pure refactor, no behavior change).
  `ProposalEvent.type` widened to admit `"request_changes"` — third-party
  `StorageBackend`/consumer code that exhaustively matches on `.type` needs updating.
  `POST /proposals/{proposal_id}/review` (REST) mirrors `/reject`'s shape exactly.
- **Note:** both backends' `proposal_event.type` `CHECK` constraint was removed
  (previously `CHECK(type IN ('accept', 'reject'))`) rather than widened again —
  `CREATE TABLE IF NOT EXISTS` never updates an existing table's constraint, so
  widening it a second time would silently break `request_changes()` on any database
  file created before this release. Databases created before this change need to be
  recreated; there is no DDL migration mechanism yet (pre-1.0/pre-alpha).
- `GET /namespaces` (SPEC §5/§12.2, ADR-0022 update, closes KI-022): new `Namespace`
  model (`ontolith.core.namespace`), `Ontology.list_namespaces()` (ungated, like
  `proposals()`/`contradictions()`), `GET /namespaces` (REST), and
  `ontolith namespace list` (CLI). Backed by SPEC §12.2's own normative `namespace`
  registry table (`id`, `created_at`, `metadata`) on both backends, seeded
  idempotently with the one namespace this project operates in today
  (`DEFAULT_NAMESPACE = "default"`) — this project remains single-namespace
  throughout (ADR-0015); no namespace-creation path was added. KI-022 is now fully
  resolved.
- **Breaking:** `StorageBackend` gained a new required Protocol method,
  `list_namespaces() -> list[Namespace]` (SPEC §12.2, ADR-0022 update, closes
  KI-022) — any third-party `StorageBackend` implementation must add it.
- `Ontology.resubmit(proposal_id, author)` (SPEC §9.1, ADR-0022 update, closes KI-027):
  the missing `changes_requested → submitted → {policy}` transition —
  `request_changes()` previously left a proposal permanently stuck once changed,
  with no way back into the review pipeline. Only the proposal's own author or
  delegate may call it; the existing payload is replayed unedited through a fresh
  policy evaluation, evaluated against the resubmission instant rather than the
  proposal's original `created_at`. A `ProposalEvent(type="resubmit")` is always
  recorded, regardless of outcome. `POST /proposals/{proposal_id}/resubmit` (REST)
  and `ontolith.resubmit` (MCP) both wrap it, returning the same `{proposal,
  decision}` shape as `POST /proposals`; CLI parity landed separately — see the
  KI-032 entry below.
- `Ontology.proposals()` (and `GET /proposals`, `ontolith proposal list --state`)
  gained a `state="pending"` query-level alias merging `require_review` and
  `changes_requested` — the other half of KI-027: even with `resubmit()` able to act
  on a `changes_requested` proposal, it was previously invisible to any single-state
  query a reviewer would naturally run. `"pending"` is an explicit opt-in, not the
  default (which remains `state="require_review"`) — a canonical reviewer loop
  (`for p in kb.proposals(): kb.accept_proposal(p.id, ...)`) assumes every returned
  proposal is reviewer-actionable, which `changes_requested` proposals are not.
- CLI `ontolith proposal accept|reject|review <id> --reviewer [--reason]` and
  `ontolith proposal resubmit <id> --author` (closes KI-032): previously
  `proposal list` was the CLI's only proposal command, so an operator using only
  the CLI could see what was pending review but had no way to act on it — every
  other primary interface (SDK, REST, MCP-for-`resubmit`) already could. `review`
  maps to `Ontology.request_changes` — SPEC §14.2 literally names the CLI command
  `proposal {list|review}`, and REST's `/review` route agrees. `accept`/
  `reject`/`review` take `--reviewer` (with `--author` accepted as an alias);
  `resubmit` takes `--author`, since there the acting principal genuinely must be
  the proposal's own author or delegate — the two options are deliberately not the
  same name for the same reason across all four commands. `resubmit` closes the
  CLI gap `Ontology.resubmit`/REST/MCP explicitly deferred to this KI when it
  shipped (see the KI-027 entry above).
- CLI `ontolith schema show [--namespace]` (partially closes KI-038): SPEC §14.2
  normatively lists `ontolith schema {show|migrate}` as CLI surface, but the CLI had
  no `schema` command at all — every other primary interface (SDK, REST, MCP) could
  already inspect a registered schema. Prints concepts, properties, and relations —
  the same field set as MCP's `ontolith.schema`/REST's `GET /schema` output (both
  already extended by KI-029 to include relations), normalized to one consistent
  attribute order rather than copying either verbatim (REST's own `PropertyOut`/
  `RelationOut` don't agree with each other on relation field order). `ontolith
  schema migrate` remains unimplemented — schema versioning/migration isn't built
  anywhere yet, only monotonic version numbering via `apply_schema` — forward-tracked
  as KI-048 rather than left implicit in KI-038's now-partial-resolved status.
- `.where()` lookup operators `__contains`/`__gt`/`__lt`/`__gte`/`__lte` (closes
  KI-039): a documented use-case example used `where(text__contains=...)` before
  `.where()` supported any lookup-operator syntax at all — every dunder-suffixed key
  raised `ValidationError` (KI-030). `__contains` does a substring match (`LIKE`,
  wildcards escaped); `__gt`/`__lt`/`__gte`/`__lte` do a numeric range comparison,
  restricted to predicates the active schema declares `Integer`/`Float` (`value_lit`
  is always stored as `TEXT`, so an unrestricted ordering comparison would silently
  compare `"9" > "10"` lexicographically) — a relation predicate is rejected the same
  way, since `SchemaIR.value_type_of()` returns `None` for one. Two different
  operators can target the same predicate (`.where(age__gte=18).where(age__lt=65)`),
  which required changing how filters are represented internally and passed to the
  backend.
- **Breaking:** `StorageBackend.entities_where`'s `predicate_filters` parameter
  changed from `dict[str, str]` to `list[tuple[str, str, Any]]` (`(predicate,
  operator, value)` triples, KI-039) — a predicate-keyed dict couldn't represent two
  different operators on the same predicate. Any third-party `StorageBackend`
  implementation must update.
- Review found `__contains` genuinely wasn't identical across backends as first
  shipped: SQLite's `LIKE` is case-insensitive by default, DuckDB's is not, so the
  same filter matched different result sets per backend. `SQLiteBackend` now sets
  `PRAGMA case_sensitive_like = ON` at connection time to agree with DuckDB's
  default. `.where(x__contains=<non-str>)`/`.where(x__gt=True)` now raise
  `ValidationError` eagerly instead of a bare `AttributeError` (the former) or
  silently accepting a bool as numeric (the latter, since `bool` is an `int`
  subclass). A leading-dunder key with an empty property name (`.where(__contains=
  "x")`) is now rejected instead of silently compiling to an unmatchable predicate —
  the exact KI-030 failure shape. Range-operator schema validation now resolves via
  `get_schema_at()` under `.as_of()` (SPEC §11.4) instead of always today's schema.
  ADR-0027 was amended (traversal remains deferred) and `docs/Ontolith_SPEC.md`
  §11.1 plus the REST/MCP/CLI filter docs, which had drifted to claim equality-only,
  were corrected. Filed separately, not fixed here: nothing validates that a
  literal's stored content actually parses as its declared `value_type` (KI-031 only
  checks the type token) — SQLite's `CAST` silently returns `0.0` for non-numeric
  stored text under a range filter where DuckDB's `TRY_CAST` excludes the row
  instead, a real, tracked cross-backend divergence (KI-049).
- Registered `Validator` plugins (SPEC §13.2) are now actually invoked — previously
  `.validate()` had zero call sites anywhere outside the protocol/plugin definitions
  themselves (closes KI-042). `Ontology`/`Ontology.connect` gain two new constructor
  parameters: `validators` (per-assertion, synchronous, blocking — runs at every point
  an assertion actually commits: `assert_literal`, `assert_ref`, `propose`/
  `propose_ref`'s auto-accept path, and `accept_proposal`/`resubmit`'s replay of a
  proposal's operations) and `completeness_validators` (whole-entity, run once per
  distinct subject touched by an accepted proposal's operations, `accept_proposal`
  only — never direct writes or any auto-accept path). The two-list split exists
  because a single per-assertion invocation point cannot serve whole-entity-completeness
  checks: an entity built up one assertion at a time is incomplete by construction until
  its last write (see ADR-0029). `RequiredFieldsValidator` gains a
  `from_schema(schema: SchemaIR)` classmethod (closes KI-041) that derives its
  `required_predicates` from the schema's own `PropertyDef.required`/
  `RelationDef.required` declarations instead of a hand-maintained, independently
  drifting mapping — wire it via `completeness_validators=[RequiredFieldsValidator.
  from_schema(schema)]` for schema-declared `required` fields to actually be enforced.
  `Validator.validate()`'s `kb` parameter type widened from the concrete `ReadOnlyView`
  to a new minimal structural `ValidatorKbView` Protocol (`plugins/ports.py`, exported
  from `ontolith.plugins`), since `Ontology`-registered validators receive the live
  `Ontology` instance itself as `kb` (trusted the same way `PolicyStrategy` already is,
  not sandboxed) rather than a capability-scoped view; `PluginRegistry`-loaded
  validators are unaffected and still have no automatic invocation point of their own —
  recorded as an explicit follow-up in ADR-0029, not a new KI.
- CLI `ontolith schema migrate <file> --author <admin>` (closes KI-048, ADR-0034):
  completes the SPEC §14.2 `ontolith schema {show|migrate}` surface KI-038 left half
  implemented. A thin wrapper reading a LinkML-aligned YAML document (ADR-0013 dialect)
  from disk and applying it as a new schema version via the existing governed
  `Ontology.apply_schema` — no new domain logic or port method. YAML-only, not
  class-DSL (no existing mechanism loads a `SchemaIR` from a class-DSL *file path*
  without dynamically executing arbitrary Python; a class-DSL schema still reaches this
  command via the existing `compile_schema()` → `to_yaml()` round-trip). Does not
  migrate/backfill existing assertion data against a changed schema — that remains
  explicitly out of scope, tracked as its own future decision.

#### Fixed
- `schema/linkml.py`'s LinkML bridge now emits `range: double` for `value_type="Float"`, not
  `range: float` (closes KI-068): real LinkML tooling treats `float` as 32-bit `xsd:float`, but
  Ontolith's `Float` is backed by Python's `float` (IEEE-754 double) throughout, so the old mapping
  understated the actual precision and diverged from `schema/rdf.py`'s own RDF/OWL bridge, which
  already mapped the identical `value_type` to `XSD.double` — a same-`value_type` inconsistency
  ADR-0036 disclosed on its own side but ADR-0013 didn't (both now updated). `from_yaml` already
  accepted `double` on import before this change; `float` stays accepted too, for backward
  compatibility with hand-authored LinkML and schemas exported by a pre-KI-068 Ontolith version.
- REST's `GET /contradictions`/GraphQL's `Query.contradictions`, `GET /proposals`/
  `Query.proposals`, and the CLI's `contradiction list --state`/`proposal list --state` all passed
  an unvalidated `state` filter straight to the backend's `WHERE state = ?` (closes KI-077, found
  during KI-076's review): an unrecognized value (a typo, wrong case, or a plausible-sounding
  synonym) silently matched zero rows instead of erroring — indistinguishable from "no results in
  that state." Every one now raises/exits on anything outside its accepted set (`{"open",
  "resolved", "all", None}` for contradictions; the 8 `Proposal.state` values plus
  `"pending"`/`"all"`/`None` for proposals; the CLI has no `"all"` value for `--state` itself,
  since `--all` is its own separate flag). `Proposal.state`/`Contradiction.state` are now named
  `ProposalState`/`ContradictionState` `Literal` aliases (`govern/proposal.py`/
  `govern/contradiction.py`) rather than inlined, so every interface — MCP's own
  `ontolith.list_contradictions` included, previously a hand-written duplicate of the same tuple —
  derives its accepted-value set from the same `get_args()` call on the shared alias instead of
  four independent copies that could silently drift from each other.
- `Ontology.flag_contradiction()`'s "extend" branch reading a malformed prior `rationale_history`
  blob (pre-existing since KI-071, found during KI-076's review): `existing.metadata.get(
  "rationale_history", [])` either raised (a non-iterable value) or, worse, silently corrupted the
  trail further on write (e.g. a bare string exploded into one entry per character). Now routes
  through the same `safe_rationale_history()` helper the read surfaces already use. Filed KI-077
  for the same unvalidated-`state`-parameter shape on REST's `GET /contradictions`/GraphQL's
  `Query.contradictions`, pre-existing and out of this fix's own scope.
- `flag_contradiction()`'s `rationale` is no longer silently dropped when extending an
  already-open contradiction (closes KI-071). Previously only the "create a new contradiction"
  branch wrote `rationale` into `Contradiction.metadata`; the "extend" branch never touched
  `metadata` at all. Both branches now write into a unified `metadata["rationale_history"]` shape
  — a list of `{"rationale", "actor", "at"}` entries, one per call that supplied a truthy
  rationale (`None`/`""` are both still treated as "none given", matching the method's
  long-standing behavior) — so a rationale given while extending is preserved alongside whatever
  was recorded at creation, and a rationale-less extend leaves prior history untouched rather than
  blanking it. `Contradiction.metadata` was never exposed by any interface, so no consumer
  depended on the old single-key `{"rationale": ...}` shape from the `create` path — it's unified
  into `rationale_history` too. No shipped interface reads `rationale_history` back yet — tracked
  as KI-075.
- **Breaking:** `StorageBackend.update_contradiction_members` gained an optional `metadata`
  parameter (implemented identically in SQLite and DuckDB), backing the `flag_contradiction` fix
  above. `Ontology` now always passes `metadata=` (`None` when no rationale was given) on the
  "extend" branch, so a third-party backend still on the old 2-argument signature raises an
  unmapped `TypeError` (not a SPEC §16 `OntolithError`) on *every* `flag_contradiction` call that
  extends an existing contradiction, rationale or not — accept a `metadata` keyword argument if
  your backend needs to keep working.
- **Breaking:** MCP now has one blanket error-handling path instead of hand-catching a handful of
  exception types per tool (closes KI-074, ADR-0014 update): a new module-level
  `_error_response(exc)` — the MCP equivalent of REST's `_handle_ontolith_error`/GraphQL's
  `process_errors` override — every tool now wraps its whole body in
  `try: ... except OntolithError as exc: return _error_response(exc)`. Closes the remaining 5 of
  10 taxonomy codes (`SchemaError`, `PolicyDenied`, `ConflictError`, `StorageError`, `PluginError`)
  that were previously unreachable from MCP entirely (escaping as an unstructured protocol
  exception with no code), and redacts `StorageError`/`PluginError` messages the same way
  REST/GraphQL already do (they interpolate raw internal exception text). Every tool's response
  shape also gains a `detail` key, matching REST/GraphQL's `{"code", "message", "detail"}` — this
  is the breaking part: any client relying on the exact 2-key `{"error", "code"}` shape gets a
  third key now, though `error`/`code` themselves are unchanged for the codes MCP already returned.
  Review found the new redaction turned a pre-existing mislabel in `Ontology.retract()` into an
  information-destroying one: an unknown `assertion_id` used to surface an actionable (if
  misclassified) `StorageError` message; blanket redaction hid it behind "An internal error
  occurred" — and, on the review-routed path an AI/MCP caller actually takes, no error surfaced
  at all, letting a phantom proposal persist. Fixed at the source — `retract()` now raises
  `NotFoundError` for an unknown `assertion_id` unconditionally, before policy is even evaluated
  — which also improves REST (`404` instead of `500`) and GraphQL, not just MCP.
- **Breaking:** MCP's error `code` values now match REST/GraphQL's shared taxonomy (closes KI-059,
  SPEC §16): 32 hand-written lowercase literals (`"auth_error"`, `"not_found"`, etc.) in
  `interfaces/mcp.py` replaced with `exc.code` from the caught `OntolithError` (or the exception
  class's own `.code` attribute where no instance is in scope), matching what REST/GraphQL already
  pass through unmodified. Any existing MCP client string-matching the old lowercase codes breaks
  on upgrade — MCP tool-schema/response stability has no formal ADR-0019-style policy yet (that ADR
  explicitly excludes `interfaces/mcp` from its scope), but this is called out regardless, the same
  as prior MCP wire-contract changes. Also found and fixed along the way:
  `ontolith.get`/`ontolith.provenance`'s "not found" responses had no `code` key at all, not just
  the wrong casing. New `tests/unit/test_cross_interface_error_codes.py` asserts REST, GraphQL, and
  MCP all report the same code for the same underlying exception type, sharing one backend across
  all three. The 5 taxonomy codes still unreachable from MCP at all (`SchemaError`, `PolicyDenied`,
  `ConflictError`, `StorageError`, `PluginError` — MCP still hand-catches per call site rather than
  one blanket mapping like REST/GraphQL) are tracked as KI-074, not fixed here.
- Bitemporal-correctness gap in `QueryBuilder.semantic()`, found while adding MCP's `as_of`
  support (KI-058): combined with `.as_of()` but no `.where()` filter, semantic search previously
  ignored `as_of_time` entirely, returning entities that didn't exist yet at that point in time. No
  prior interface could trigger this (REST/GraphQL never exposed `as_of`), so it was unreachable
  until MCP's own `as_of` addition made it reachable. `.semantic()` combined with `.as_of()` still
  only excludes entities that didn't exist by that time — it does not make the vector search itself
  bitemporal, since the vector index holds one embedding per entity with no historical versions;
  documented explicitly on `QueryBuilder.semantic()` and `query_tool`'s own `as_of` docstring.
- `Ontology.retract()` — the codebase's most heavily-governed write path — is now
  reachable from every shipped interface, not just the SDK (closes KI-057, ADR-0039):
  `POST /assertions/{id}/retract` (REST, `acting_as` as an optional query parameter),
  a `retract` mutation (GraphQL), a new top-level `ontolith retract <id> --author <id>
  [--acting-as <id>]` CLI command, and `ontolith.retract` (MCP, `propose` capability
  tier — the same tier as `ontolith.propose`/`ontolith.flag_contradiction`, unlike the
  reviewer-only `resolve_contradiction`, which stays MCP-excluded). All four are thin
  wrappers with no new domain logic.
- **Security, Breaking:** `create_graphql_app`'s `introspection` parameter now
  defaults to `False` (closes KI-056, amends ADR-0037). Introspection queries are
  self-referentially recursive over the schema's own type graph, and neither
  `QueryDepthLimiter` nor `MaxTokensLimiter` can bound that recursion (verified
  directly — `QueryDepthLimiter` hardcodes an introspection carve-out in
  *strawberry-graphql's own* depth-limiting validator, not graphql-core, which
  has no depth validator at all); an anonymous caller previously had an
  unauthenticated, unbounded-recursion amplification vector with no mitigation.
  Pass `introspection=True` to opt back in — note `graphql_ide` still defaults
  to serving GraphiQL, so pass both together for a working interactive dev
  experience. Also new: `MaxAliasesLimiter`/`QueryDepthLimiter` are wired into
  every schema unconditionally, capping alias count and query depth — *reduces*
  the cross-interface DoS vector KI-052's async resolver conversion introduced
  (measured: 15 concurrent worker threads per max-alias request against the
  shared anyio pool's default 40-thread capacity, so this doesn't eliminate the
  vector, only shrinks the amplification ratio from ~200 aliased fields to 15).
  Requires `strawberry-graphql>=0.316` (bumped from `>=0.219` — the
  factory-callable extension pattern this fix uses raises `TypeError` at
  request time on older releases).
- **Security, Breaking:** `require_admin` (`ontology.py`) now rejects AI-kind
  principals regardless of their configured capability (closes KI-053, ADR-0038),
  matching every other capability-tier gate in the codebase. Previously a
  misconfigured AI principal with `default_capability="admin"` could call
  `issue_token()` for a human principal and authenticate as them over
  REST/GraphQL, bypassing every AI-kind guard on direct write, review, and
  contradiction resolution — any deployment relying on that (misconfigured)
  behavior now gets `CapabilityError` instead. CLI's `principal create` now
  requires `--author` (naming an existing admin) once a database has any
  principal at all, mirroring REST's already-correct external-gate pattern, with
  a bootstrap exception only for a database's first-ever principal (closes
  KI-054, same ADR) — **any script or runbook calling `ontolith principal
  create` without `--author` against a non-empty database now exits 1** instead
  of succeeding.
- **Security:** `PluginRegistry.register()` (`plugins/registry.py`) now logs a warning, on
  successful registration, when a plugin's manifest declares `capabilities.network=True` or
  `capabilities.filesystem=True` (amends KI-014's still-open half, ADR-0015). Only
  `capabilities.storage` is actually enforced — plugins run in-process with no process/wasm
  isolation — so declaring these was previously silent, leaving an operator deciding whether to
  register the plugin with no signal, at the moment that matters, that the declaration does
  nothing. Three of the four shipped reference plugins (`CsvImporter`, `JsonExporter`,
  `RdfExporter`) declare `filesystem=True` and now log this on every registration — expected, not
  a regression. Visibility only, not enforcement: SPEC §17's MUST for network/filesystem
  isolation remains unmet. Real process/wasm isolation is unchanged, tracked separately per the
  Implementation Plan's existing phasing.
- GraphQL resolvers (`interfaces/graphql.py`) no longer block the ASGI event loop
  (closes KI-052, amends ADR-0037). Every `Query`/`Mutation` field, plus
  `EntityType.assertions` and `create_graphql_app`'s `_get_context`, is now
  `async def`; the actual blocking `kb`/`kb.backend` calls are factored into sync
  helper functions and dispatched via `starlette.concurrency.run_in_threadpool`.
  Previously every resolver ran inline on the event loop (unlike REST, whose
  routes Starlette dispatches to a thread pool automatically), so a slow resolver
  blocked *all* concurrent traffic, not just database-bound requests — measured
  directly: three concurrent requests against a deliberately slowed resolver went
  from ~0.92s (serialized) to ~0.33s (overlapping, matching REST).
- **Breaking:** KI-033's party guard and KI-043's capability floor now apply to a
  `retracted`/`superseded` contradiction member exactly as they already did to a
  `flagged` one (closes KI-051, amends ADR-0030). Both guards previously gated on
  `status == "flagged"` before ever checking contradiction membership, so a member
  ADR-0031 had already terminalized while its contradiction stayed `open` was
  completely exempt — a party could retract it, or a below-floor neutral principal
  could auto-accept retracting it, with no governance applied. `_open_contradiction_if_member`/
  `_require_capability_to_retract_contradiction_member` (renamed from
  `..._flagged_member`/`..._if_flagged_member` for accuracy) now key off contradiction
  membership alone, independent of the target's own status. Separately, `retract()`
  now no-ops (no status/event write) when re-retracting an already-`retracted` target,
  closing a narrower, contradiction-independent event-misattribution gap — deliberately
  **not** extended to an already-`superseded` target, unlike `resolve_contradiction()`'s
  own loser-loop no-op (KI-044): explicitly retracting a `superseded` assertion via
  `retract()` remains a real, event-recording transition this codebase already relies
  on (`test_events_ordered_oldest_first`).
- **Breaking:** `flag_contradiction()` now rejects opening a **new** contradiction whose
  two founding members are both already `retracted`/`superseded` (closes KI-050,
  ADR-0035) — `resolve_contradiction()` already rejects a terminal-status winner
  candidate (KI-044, ADR-0031), so an all-terminal pair at creation time opened a
  contradiction with zero eligible winners, forcing a follow-up write before it could
  ever be resolved. Mirrors `resolve_contradiction()`'s own check in mechanism
  (`ValidationError`, not a capability gate — this is a structural validity issue, not a
  capability shortfall) and in the terminal-status set checked. Only guards contradiction
  *creation*; extending an *already-open* contradiction with an all-terminal pair remains
  permitted (ADR-0031's own deliberate escape hatch for naming a terminal assertion for
  audit/context, unchanged). Checked against the fresh, in-transaction reads KI-045
  already established, so it also covers the race variant (a concurrent
  retract()/supersession terminalizing both named assertions between read and write),
  not just an explicit two-terminal-ids call.
- **Breaking:** `assert_literal`/`propose` now reject a literal whose `value` doesn't
  actually parse as its (already token-matched, KI-031) declared `value_type` (closes
  KI-049, amends ADR-0028) — e.g. `assert_literal(..., "unknown", "Integer", ...)`
  previously succeeded since `value_type="Integer"` matched the schema even though
  `"unknown"` isn't a valid integer; now raises `ValidationError`. A dedicated regex for
  `Integer`/`Float` (not bare `int()`/`float()`, which accept underscore separators,
  whitespace, and — for `float()` — `"inf"`/`"nan"`, none of which cast consistently
  across both backends' KI-039 SQL paths); Python's `fromisoformat` grammar for
  `Date`/`DateTime` (`Date` rejects a string carrying a time component);
  case-insensitive `"true"`/`"false"` only for `Boolean` (not `"1"`/`"0"`, a deliberate
  choice); `json.loads()` for `JSON` (rejecting the non-standard `NaN`/`Infinity`
  constants too); `URI` accepts LinkML's `uriorcurie` shape (ADR-0013) — a full URI or a
  CURIE, not a strict RFC 3986 parse. `Text` has no format to validate. Enforced at
  submission time only, same as the token check beside it — not retroactive against
  already-stored data (no migration mechanism, KI-048) and not re-run at proposal
  replay, matching that check's own established precedent.
  `entities_where()`'s `TRY_CAST`/`CAST` defensive handling (KI-039) is unchanged and
  still necessary for pre-existing data.
- **Breaking:** `.trust_at_least()`/`entities_meeting_trust` now compare a delegated
  assertion's *effective* trust — `min(author.trust_level, acting_as.trust_level)` —
  instead of the author's raw `trust_level` alone (closes KI-047). Matches
  `govern/policy.py`'s existing effective-trust formula for the same assertion, by
  analogy with SPEC §8.4's capability rule; a low-trust delegate acting as a high-trust
  principal (or vice versa) is now scored consistently between policy evaluation and
  query-time filtering, which it previously wasn't. Cross-backend divergence, verified
  before implementing: SQLite's `min(a, b)` is the scalar two-argument form; DuckDB's
  `min(a, b)` is aggregate-only and returns a list for two scalar args, so DuckDB's
  query uses `least(a, b)` instead. A dangling `acting_as` (no resolvable delegate)
  fails open, falling back to the author's own `trust_level`, deliberately unlike
  `_resolve_delegation`'s fail-closed behavior for the same input at write time.
  Non-delegated assertions are unaffected. New conformance vectors
  (`TestTrustAtLeastDelegationAttenuation`) cover both attenuation directions, the
  inclusive threshold boundary, and the dangling-delegate fallback. See ADR-0033.
- **`DuckDBBackend` now serializes connection access across threads with a
  `threading.RLock`, mirroring `SQLiteBackend`'s KI-023 fix (closes KI-046).**
  DuckDB's own DB-API `threadsafety` level is 1 ("threads may share the module, but not
  connections") — the identical constraint that drove SQLite's fix — but `DuckDBBackend`
  had no lock and no guard of any kind, so two concurrent transitions on the same
  connection didn't serialize. All 40 public methods now carry the same `@_synchronized`
  decorator `SQLiteBackend` uses; `begin()`/`commit()`/`rollback()` acquire/release the
  lock with the identical asymmetric-release pattern. No `_in_transaction` flag needed
  (unlike SQLite) — DuckDB's own native autocommit already makes standalone writes
  durable without one. New threaded regression tests
  (`tests/unit/test_duckdb_backend.py::TestConcurrency`) mirror SQLite's own KI-023
  coverage; four of five confirmed to fail against the pre-fix code, including a new
  test proving the worst pre-fix consequence: silent data corruption on concurrent
  reads (wrong/missing rows, no exception raised at all), not just an unguarded
  transaction span. See ADR-0032.
- **`flag_contradiction()` no longer has a TOCTOU window between reading its target
  assertions/existing open contradiction and writing its decision (closes KI-045).**
  Both target assertions and any existing open contradiction for their `(subject,
  predicate)` were read before opening the write transaction — a concurrent
  `retract()`/supersession landing in the gap meant the KI-034 terminal-status guard
  could still see a stale `active` status and resurrect an assertion that had since
  become terminal, and a concurrent `resolve_contradiction()` closing the open
  contradiction in the gap meant this call could extend an already-`resolved`
  contradiction (`update_contradiction_members()` has no state guard of its own).
  Mechanically identical to KI-035's fix for the four proposal-transition methods: the
  reads and the decisions built on them now happen as the first statements inside the
  transaction, re-read fresh; only the principal/capability check stays outside (pure
  identity, not state that races — verified no code path mutates a principal's
  capability after creation). New conformance vectors (`TestFlagContradictionTOCTOU`)
  simulate three races deterministically via the same `_RacingClock` test double KI-035
  introduced, without real threads: a concurrent `retract()`, a concurrent
  `resolve_contradiction()` closing the contradiction being extended, and a concurrent
  `flag_contradiction()` opening a competing contradiction for the same
  `(subject, predicate)` — all three confirmed to fail without the fix.
- **Breaking:** `resolve_contradiction()` now rejects a winner candidate whose status is
  already `retracted` **or `superseded`** with `ValidationError` instead of reactivating
  it to `active` with a closed `valid_to` (closes KI-044) — that combination silently
  undoes a governance action's close of the assertion's validity window with no new
  write recording it reopened. Mirrors the existing "winner not a member" check exactly
  (same exception type, same validation loop, before any write, checked only after the
  party-to-contradiction guard has cleared for every member so error precedence is
  deterministic). Also closes a related gap: extending an open contradiction previously
  un-terminalized a `superseded` member back to `flagged` (the KI-034 fix only ever
  covered `retracted`) — `_apply_with_conflict_routing`'s extension branch now skips
  both, and `resolve_contradiction()`'s own loser loop does the same instead of
  overwriting a `superseded` loser to `retracted` and misattributing a second event to
  the resolver. Retraction/supersession is now terminal everywhere `resolve_contradiction()`
  and its feeder write paths are concerned (party-to-contradiction guard KI-033,
  conflict-routing extension KI-034, `flag_contradiction()`'s own re-flag guard, and now
  the winner/loser checks here) — except `retract()` itself, which still doesn't
  recognize a `superseded` member as governed the way it does a `flagged` one, filed
  separately as KI-051. A resolver who wants a terminal value active again must submit
  it as a new assertion instead — for a `time_varying` predicate this means
  `flag_contradiction()` specifically, not a bare re-assert (which comes back `active`
  and never rejoins the contradiction; the bare-reassert shortcut only ever applies to
  `static` predicates). A contradiction whose every *existing* member ends up terminal
  has no eligible winner until one more assertion lands (not a permanent dead end — see
  ADR-0031 for the two-mechanism escape hatch); `flag_contradiction()` can also open a
  brand-new contradiction with no eligible winner among its two founding members from
  the start, filed separately as KI-050. See ADR-0031.
- **Breaking:** `retract()`/`resubmit()` now route to review, instead of auto-accepting,
  when the target is a `flagged` member of an open contradiction and the retracting
  principal doesn't meet the same `review`/`admin` capability + non-AI floor
  `resolve_contradiction()` already enforces (closes KI-043) — previously any
  `write`-capability principal (party to neither disputed value) could retract one side
  of a dispute outright, reaching close to the same effective outcome as
  `resolve_contradiction()` at a materially lower floor. A `write`-capability
  principal's retraction of such a member now returns a `require_review` proposal
  instead of taking effect immediately; a `review`-capable, non-AI principal can accept
  it via `accept_proposal()`. Ordinary retraction (target not a flagged contradiction
  member) is unaffected — still just `write`. Delegation attenuates the effective
  capability (`min(principal, delegating)`, SPEC §8.4) same as direct writes. See
  ADR-0030, which also documents why routing to review (rather than raising
  `CapabilityError` outright, an earlier version of this fix) was necessary to avoid
  leaving a `write`-capability principal worse off than a lower-capability one for the
  same action.
- **`assert_literal`/`assert_ref`/`propose`/`propose_ref` now reject a predicate-kind
  mismatch (closes KI-040):** nothing previously stopped a literal write against a
  schema-declared relation predicate, or a ref write against a schema-declared
  property predicate — `assert_literal(subj, "Person.employer", "Acme Corp", "Text",
  ...)` silently succeeded even when `Person.employer` was declared a relation. New
  `SchemaIR.kind_of(predicate)` resolves the declared kind; `_require_known_predicate`
  gained a required `expected_kind` keyword (not inferred from `value_type`'s presence,
  so a future write path can't silently skip the check by omitting it) and raises
  `ValidationError` on mismatch. No-op for a schema-less namespace, matching this
  method's existing precedent. Fixing this surfaced a real pre-existing bug in two
  unrelated conformance tests that had been writing `assert_ref` against a
  property-declared predicate, silently permitted before this fix.
- **Breaking:** `StorageBackend.entities_meeting_confidence`/`entities_meeting_trust` (port
  + both backends) gained a required `candidate_ids: frozenset[str] | None = None`
  parameter (KI-037) — any third-party `StorageBackend` implementation must add it, since
  `QueryBuilder` now passes it as an explicit keyword on every call (including `None`).
- **`.min_confidence()`/`.trust_at_least()` now exploit an already-narrowed `.where()`/
  `.semantic()` candidate set instead of always scanning the full concept (closes
  KI-037).** `QueryBuilder` passes the new `candidate_ids` hint only when `.where()`/
  `.semantic()` narrowed the base candidate set to at most `_CANDIDATE_HINT_MAX` (1000)
  entities — unbounded, a low-selectivity `.where()` predicate matching thousands of
  entities would make encoding the hint cost more than the scan it saves. Both backends
  use the hint: SQLite binds the id set as a single JSON-encoded parameter (`json_each`)
  rather than one placeholder per id; `DuckDBBackend` uses the equivalent `unnest()`
  construct. Measured on the same machine, same query, same fixture, code-only diff (50k
  entities, single-candidate `.where()` match): several times faster (absolute latency
  is hardware-dependent; see `tests/benchmarks/test_hybrid_query.py`'s like-for-like
  pair for a reproducible comparison rather than a point-in-time number here).
  Bounding the hint's size, not just adding it, is what makes it a reliable win — an
  earlier, unbounded version of the DuckDB hint measured over 100x *slower* for a
  candidate set of a few thousand against a 10k-entity concept. New conformance vectors
  pin the backend-agnostic contract (`(full ∩ candidate_ids) <= narrowed <= full`, which
  holds whether or not a given backend actually narrows); backend-specific unit vectors
  (SQLite and DuckDB) pin that both current backends' own implementations genuinely
  narrow, including an empty-candidate-set short-circuit — all confirmed to fail without
  the fix.
- **`.min_confidence()`/`.trust_at_least()` now respect `.as_of()` (closes KI-036).**
  `QueryBuilder._apply_confidence_trust_filters` never read `self._as_of_time`, so
  `kb.as_of(t).query(...).min_confidence(...)`/`.trust_at_least(...)` always checked
  current-active assertions regardless of `t` — an entity could pass the `.where()` half
  of a bitemporal query as it existed at `t`, then get filtered by confidence/trust values
  that only became true later (or that existed at `t` but were since superseded/retracted).
  `StorageBackend.entities_meeting_confidence`/`entities_meeting_trust` (port + both
  backends) gained an `as_of_time` parameter, mirroring `entities_where()`'s existing
  bitemporal-window branch; `QueryBuilder` now threads `self._as_of_time` through both.
  `trust_level` itself is always the principal's current value, not a historical one — no
  code path updates a principal's `trust_level` after creation, so there is no historical
  value to reconstruct; only which assertion counts as qualifying is bitemporally scoped.
  A new regression guard (`tests/unit/test_principal_trust_immutability_invariant.py`) fails
  if a `principal` table mutation path is ever added, since that would break this shortcut.
  `status` itself is not bitemporally versioned, so a flagged assertion is excluded
  regardless of `t`, even before it was flagged — matching `entities_where()`'s own default.
  Conformance vectors (`TestAsOfConfidenceTrust`, 22 cases across both backends) cover a
  retraction boundary and a schema-declared time_varying supersession boundary per filter,
  plus — per review — vectors isolating each of the four temporal clauses individually
  (backdated-but-not-yet-known, future `valid_from`, the `valid_to` half-open boundary,
  flagged-exclusion) and vectors combining `.as_of()` with `.where()` and with both filters
  chained together; every clause confirmed individually load-bearing via mutation testing.
  Review also found that `.trust_at_least()` ignores delegation attenuation (SPEC §8.4) —
  pre-existing, not introduced here, filed separately as KI-047.
- **`accept_proposal`/`reject_proposal`/`request_changes`/`resubmit` no longer have a TOCTOU
  window between validating a proposal's state and writing its transition (closes KI-035).**
  All four read the proposal, validated its current state, and ran policy evaluation before
  ever opening the write transaction — only the writes themselves were atomic. Two concurrent
  calls that both observed the same pre-transition state (e.g. an `accept_proposal` racing a
  `reject_proposal`, both reading `require_review`) could both pass validation and both reach
  their write, replaying the same proposal's operations twice under an auto-accepting policy.
  `_require_reviewer` split into `_require_reviewer_principal` (reviewer-identity checks, safe
  before the transaction) and `_require_pending_proposal` (re-reads the proposal fresh and
  checks self-review/state — now called as the first thing inside the transaction, in all
  three reviewer-side methods); `resubmit` keeps its original pre-transaction checks as an
  optimistic fast-fail but adds an authoritative re-check inside its own transaction before
  any write. Mirrors `resolve_contradiction`'s own KI-026 fix for the identical bug shape.
  New conformance vectors (`TestProposalTransitionTOCTOU`) deterministically simulate the race
  per method via a `_RacingClock` test double, without real threads — confirmed to fail
  without the fix. Pre-existing, not yet triggered by any test or reported incident
  (single-threaded usage today); found while re-reviewing the KI-027 `resubmit()` fix. Review
  found the identical TOCTOU shape in `flag_contradiction()` (filed separately as KI-045, not
  fixed here — KI-035 itself scopes to the four proposal-transition methods) and that
  `DuckDBBackend` has no equivalent of `SQLiteBackend`'s KI-023 concurrency lock, so this
  fix's serialization guarantee is proven airtight only for SQLite today (filed as KI-046).
- **`retracted` now stays terminal when an open contradiction is extended by a new disputed
  value (closes KI-034).** `retracted` is meant to be a terminal status everywhere in the
  codebase (SPEC §5's append-only lifecycle) — this was the path reachable from the
  write/proposal pipeline where it wasn't. `Ontology._apply_with_conflict_routing`'s "extend
  an already-open contradiction" branch (not `govern/conflict.py`'s pure `route()`, which is
  bypassed entirely once a contradiction is already open) unconditionally re-flagged every
  existing member alongside the incoming assertion, including one that had since been
  legitimately retracted (e.g. by a neutral third party via `retract()`, KI-033) —
  resurrecting it back to `flagged`. The flagging loop now skips the status write (and its
  event) for any member whose current status is already `retracted`, and now raises
  `NotFoundError` for a missing member instead of silently falling through into an unguarded
  write, matching `resolve_contradiction`'s own KI-026 precedent (found in review). The
  member's id is deliberately left in the `Contradiction`'s own `member_ids` — that list
  isn't audit-only, it's also `resolve_contradiction`'s winner-eligibility set and
  `_reject_retract_if_party_to_contradiction`'s scan set — only the re-flagging write is
  skipped. Review found the identical resurrection bug in `flag_contradiction()`'s own,
  separate flagging loop (SPEC §14, MCP `ontolith.flag_contradiction`, reachable at only
  `propose` capability including by an AI principal) — fixed the same way here, now also
  guarding `superseded`. `resolve_contradiction` itself no longer re-emits a duplicate,
  resolver-misattributed `retracted` event for a loser that's already `retracted`. Filed
  **KI-044** (backlog, not fixed here): `resolve_contradiction()` can still pick an
  already-`retracted` member as the *winner*, reactivating it to `active` with a closed
  `valid_to` window — a broader winner-eligibility question this fix doesn't expand into.
  Found while investigating KI-033, not introduced by it — pre-existing.
- **`Ontology.retract()` now rejects retracting a flagged member of an open contradiction
  when the retracting principal (author or delegate) is a party to that contradiction —
  author or delegate of *any* member, not just the target being retracted (closes
  KI-033).** `retract()` routes through the normal policy path like any other write; a
  human principal with `write`/`review`/`admin` capability auto-accepts under
  `ThresholdPolicy` with no contradiction awareness at all, so a principal who authored
  one side of a disputed static fact could retract the *opposing* member directly —
  reaching the same one-sided outcome `resolve_contradiction`'s KI-026 self-resolution
  guard already blocks, just through a side door with no notion of contradictions.
  Retracting your own losing member is blocked too, for the same "any member" reasoning
  KI-026 established: giving up your own side unilaterally ends the dispute in the other
  party's favor just as much as picking your own side as the winner would. The new
  `_reject_retract_if_party_to_contradiction` check runs inside the same transaction that
  performs the retraction, before any of that transaction's writes land (both in
  `retract()`'s own auto-accept branch and in `_replay_proposal_operations`'s `retract`
  branch, shared by `accept_proposal`/`resubmit`) so a contradiction opened or extended
  concurrently can't slip past it — mirroring `resolve_contradiction`'s own race-safety
  reasoning. The checked party set also covers the *accepting reviewer*, not just the
  proposal's original author/delegate: found in review, a reviewer who is themselves a
  party to the same contradiction could otherwise reach the identical one-sided outcome by
  approving a neutral principal's retract proposal instead of retracting directly. A
  missing contradiction member now raises `NotFoundError` rather than silently skipping
  the check for it, matching `resolve_contradiction`'s own precedent (also found in
  review). A neutral third party (author/delegate of no member) is unaffected;
  `resolve_contradiction` remains the correct way to actually close out a disputed fact —
  whether a neutral `write`-capability principal retracting a disputed member should
  itself require `resolve_contradiction`-grade capability is a separate question, tracked
  as KI-043.
- **Breaking:** `QueryBuilder.where()` no longer silently no-ops on relation-traversal
  filter keys (closes KI-030) — `.where(employer__name="Acme Corp")`, an example the class
  docstring itself advertised as working, compiled into an unreachable predicate string
  and always returned an empty result with no error. Dunder-containing keys (`__`) now
  raise `ValidationError` at `.where()` call time instead, naming the offending key and
  explaining that neither multi-hop traversal nor lookup operators are implemented
  (ADR-0027, KI-039) — a caller that previously got `[]` back for such a key now gets an
  exception (MCP: `{"error": ..., "code": "validation_error"}`; REST `POST /query`: `400`
  instead of `200` with an empty list). Separately, `StorageBackend.entities_where()`
  (both backends) now matches a filter value against either `value_lit` or `value_ref` via
  a `UNION ALL` of two indexed point lookups (a new `idx_assertion_pred_ref` index backs
  the `value_ref` arm), so direct relation-target-id equality (`.where(employer="org-123")`)
  actually returns matches — previously it silently matched nothing, since only
  `value_lit` was ever compared, and an initial `value_lit = ? OR value_ref = ?` version of
  this fix was reworked before merge after it was measured to fall back to a full table
  scan on SQLite. Docstrings on `QueryBuilder`/`.where()`/`StorageBackend.entities_where()`
  now state the real contract; SPEC §11.1, the PRD walkthrough, and a use-case doc example
  were corrected to match (see ADR-0027).
- MCP `ontolith.schema` and `GET /schema` now include each concept's `relations`, and
  each property's `cardinality` (closes KI-029) — both were previously omitted entirely,
  so an agent or REST client had no way to see that a relation like `Person.employer`
  exists, whether it's `time_varying`, or its cardinality — the information that
  predicts supersession vs. contradiction on a subsequent proposal (SPEC §10.1,
  ADR-0017). REST's `ConceptOut` gained a new required `relations: list[RelationOut]`
  field (name, target concept, cardinality, required, temporality, inverse) alongside
  the existing `properties`, mirroring `PropertyOut`'s shape (which itself gained
  `cardinality`) — code constructing `ConceptOut`/`PropertyOut` directly (not part of
  the public API surface per ADR-0019 — neither is exported from `interfaces.rest`)
  must now supply the new fields.
- **Breaking:** `assert_literal`/`propose` now raise `ValidationError` when the caller's
  `value_type` doesn't match the schema-declared `PropertyDef.value_type` for `predicate`
  (closes the `value_type` half of KI-031) — previously a predicate declared
  `value_type: Integer` silently accepted a literal written with `value_type="Text"` (or
  any other mismatched, case-sensitive-mismatched type), with no error anywhere.
  `SchemaIR` gained `value_type_of(predicate)`; `assert_ref`/`propose_ref` are unaffected
  (relations have no `value_type`), and no check fires for a namespace with no registered
  schema. This can break a schema-driven CSV import (`plugins/reference/csv_importer.py`)
  that previously relied on its `value_type` column defaulting to `"Text"` for every row —
  under a registered schema whose properties aren't all `Text`, that default may now raise
  mid-import. `required` remains unenforced anywhere in this codebase — ADR-0028 records
  the decision to keep it out of core (SPEC §4 assigns it to the validator layer; a
  per-write core gate is structurally the wrong shape for a check that's necessarily
  cross-assertion), and corrects an inaccurate first-draft claim that the existing
  `RequiredFieldsValidator` plugin already covered it — it doesn't read the schema's
  `required` field (KI-041), and no code path invokes any `Validator` plugin at all
  (KI-042).
- **Breaking:** `StorageBackend` gained two new required Protocol methods,
  `entities_meeting_confidence(namespace, concept, threshold) -> set[str]` and
  `entities_meeting_trust(namespace, concept, min_trust) -> set[str]` (KI-028) — any
  third-party `StorageBackend` implementation must add them.
- **`QueryBuilder.min_confidence()`/`.trust_at_least()` no longer issue one backend round
  trip per candidate entity (closes KI-028)** — reintroduced the same N+1 pattern KI-001
  fixed for `.where()`, apparently unnoticed when the two filters shipped alongside
  `.semantic()` as part of KI-018's hybrid retrieval. `entities_meeting_confidence`/
  `.entities_meeting_trust` (both backends) push each filter down to a single
  `(namespace, concept)`-scoped query, replacing the Python-side per-entity
  `assertions()`/`get_principal()` loop — a query bound to the candidate id list instead
  was tried and reverted, since its parameter count scales with data size (hits SQLite's
  bound-variable limit outright on large concepts; costs DuckDB linear per-parameter bind
  overhead). New benchmarks (`tests/benchmarks/test_hybrid_query.py`) and conformance
  vectors (`conformance/test_confidence_trust_filters.py`, covering both backends — the
  pre-existing unit tests only ever exercised SQLite) close the gap that let the original
  regression ship unbenchmarked.
- **Breaking:** `Ontology.issue_token(principal_id, author)` now returns
  `tuple[str, str]` (`(token, credential_id)`) instead of a bare `str` (closes KI-024,
  update to ADR-0014). `issue_token_route` (REST) and `principal issue-token` (CLI)
  previously recovered the newly-issued credential's id via a second,
  non-transactional `list_tokens(...)[0]` call — a concurrent token issuance for the
  same principal in that gap could return a mismatched `credential_id` alongside the
  correct raw token. The credential's id is already known when `issue_token` persists
  it, so both callers now get it directly with no second lookup.
- **`Ontology.create_principal(kind="ai", owner=None)` now raises the documented
  `ontolith.core.errors.ValidationError`** instead of a raw pydantic `ValidationError`
  leaking out of `Principal`'s own model validator. Found while wiring `POST /principals`
  (ADR-0022): REST's error mapping only handles `OntolithError` subtypes, so this would
  have surfaced as an unhandled 500 with no SPEC §16 envelope. The CLI's blanket
  `except Exception` had masked the same gap. `conformance/test_accountable_owner.py`'s
  matching vector tightened from `(ValueError, StorageError)` to `ValidationError`
  specifically, now that every backend gets one consistent exception type here.
- **`POST /principals`'s `kind`/`auth_method`/`default_capability`/`trust_level` fields
  are now typed to match `Principal`'s own `Literal`/bounded constraints** instead of
  plain `str`/`int` — an invalid value (e.g. `kind="wizard"`, `trust_level=99`) previously
  skipped Pydantic's own validation and hit the same unmapped-pydantic-error class the
  `create_principal` fix above closed for `owner`, just via a sibling field instead.
  Found in review; closed without any `Ontology`-layer change.
- **`StorageBackend.get_credentials_for_principal` (SQLite + DuckDB) now tiebreaks on
  `id DESC` in addition to `created_at DESC`** — two credentials issued in the same
  timestamp tick previously had no deterministic order, so `POST /principals/{id}/tokens`
  recovering the just-issued credential's id via `list_tokens(...)[0]` could return the
  wrong one. A narrower residual race under genuinely concurrent issuance (not just a
  coarse timestamp) is tracked as KI-024.
- **MCP read tools now require authentication (closes KI-021):** `ontolith.schema`,
  `ontolith.get`, `ontolith.query`, and `ontolith.provenance` previously took no `token`
  parameter and resolved no principal at all, contradicting SPEC §8.3 ("`read`/`query`:
  required for any retrieval") — any MCP client could call them with zero credentials.
  All four now take `token: str`, resolved via the same `AuthProvider` `propose`/
  `flag_contradiction` already use, returning the same `auth_error` shape on failure.
  Read-only, information-disclosure severity; no write/capability-escalation impact.
  Documented as an update to ADR-0014.
- SQLite backend now opens its connection with `check_same_thread=False` — an ASGI
  server (the new REST interface) dispatches requests on a different OS thread than the
  one that constructs the backend, which stock `sqlite3` blocks regardless of whether
  the access is ever actually concurrent. This flag only lifts that check; concurrent
  access is now serialized separately (see KI-023 below), not by this flag
- **SQLite backend is now thread-safe under genuinely concurrent access (closes KI-023):**
  a `threading.RLock` now guards every `SQLiteBackend` method — `begin()` holds it for
  the full span of an explicit transaction; every other public method acquires it for
  its own call, reentrant on the same thread so calls made from inside a
  `transaction()` block don't self-deadlock. Previously, two genuinely concurrent
  requests (the exact shape an ASGI worker threadpool produces) could interleave
  `BEGIN` calls, raising a raw, unmapped `sqlite3.OperationalError` instead of the
  SPEC §16 error envelope. `commit()`/`rollback()` release the lock asymmetrically
  (commit only on success, rollback always) — an `ontolith-reviewer` pass on the first
  version of this fix caught that releasing unconditionally in both double-released the
  lock on a commit failure, masking the real `StorageError` behind a `RuntimeError` and
  leaving `_in_transaction` stuck; both the fix and a dedicated regression test for that
  failure mode are documented as an update to ADR-0010. New `ThreadPoolExecutor`-based
  regression coverage in `test_sqlite_backend.py` confirmed reproducing both failures
  against the respective pre-fix code before verifying each fix
- **HIGH:** `as_of(t)` excluded flagged assertions by current status instead of
  status-at-t; since flagging never sets `valid_to`, once any contradiction had ever
  touched a `(subject, predicate)`, `as_of(t)` returned nothing for it at any t, including
  times before the dispute existed. Fixed by recording a `flagged` event for the newly
  incoming assertion in a fresh contradiction (previously only the pre-existing member
  got one) and reconstructing flagged-status-at-t from `assertion_event` instead of
  trusting current status
- **HIGH:** `accept_proposal`/`reject_proposal` now reject a reviewer who is the
  proposal's own author or delegate (`acting_as`) — self-review, including via
  delegation chain, was previously possible for a misconfigured principal with review
  capability. (`resolve_contradiction` was incorrectly believed to share this guard at
  the time — it didn't, and wasn't fixed until KI-026, below.)
- `ontolith.provenance` (MCP) and `flag_contradiction` fetched and deserialized every
  assertion in the KB to find one or two rows by ID; both now use the indexed
  `get_assertion(id)` lookup
- `assertions()`'s composite index led with `namespace`, which no query filters on
  (single-namespace today), making it unusable — confirmed via `EXPLAIN QUERY PLAN` (full
  `SCAN`, not `SEARCH`). Added indexes matching the actual filter shapes in both backends
- **HIGH:** `resolve_contradiction()` had no self-resolution guard (KI-026, found in a
  whole-project audit) — a reviewer who authored one of a contradiction's disputed member
  assertions could pick their own value as the winner, unilaterally settling a dispute they
  were a party to. `accept_proposal`/`reject_proposal`/`request_changes` already blocked this
  via their shared self-review check; `resolve_contradiction` now does too, and — unlike the
  other three, which only check the specific action being taken — checks every member of the
  contradiction, not just the winner, since an interested party shouldn't get to pick against
  their own losing entry either. `docs/adr/ADR-0022-rest-write-review-admin.md` incorrectly
  claimed this guard already existed; corrected.

#### Documented
- SPEC §18 observability (metrics/events/structured logs) is scoped to M4, not built
  opportunistically ahead of it and not declared out of scope through 1.0 (closes KI-064,
  ADR-0044): `observe/` stays an empty package for now, but the Implementation Plan's M4 scope
  column — which never named observability at all — now does, and the architecture (a single
  `Clock`/`IdProvider`-style port, `govern/policy` still emits nothing itself) and a priority
  order (structured correlated logs, then the four named lifecycle events, then the seven-metric
  surface) are decided ahead of M4 so the milestone doesn't have to re-litigate them. No code
  changes — a scoping decision, not a feature.

#### Security

- MCP's `create_mcp_server()` gains an opt-in `require_header_token: bool = False` keyword-only
  parameter (closes KI-073, ADR-0014 update) — when set, an HTTP (SSE/streamable-HTTP) deployment
  can require the `Authorization` header outright instead of merely preferring it (KI-067): an
  absent header now fails the call the same way no credential at all would, even when the caller
  still supplies a valid `token` argument. `False` by default — the argument fallback KI-067 added
  is what keeps stdio transports (no header channel exists there) usable at all, so this is a
  strictly opt-in hardening for HTTP deployments, not a fix for a live vulnerability. A malformed
  header still fails closed either way, unchanged from KI-067.
- `pydantic`, `typer`, `python-ulid`, `fastapi`, `duckdb`, `uvicorn`, and `pyyaml` now all carry an
  upper version bound (closes KI-070) — `pydantic>=2.0,<3.0`, `typer>=0.9,<1.0`,
  `python-ulid>=2.0,<4.0`, `fastapi>=0.110,<1.0`, `duckdb>=1.0,<2.0`, `uvicorn>=0.27,<1.0` (both
  places it's declared — the `rest` and `graphql` extras each list it independently),
  `pyyaml>=6.0,<7.0`, extending KI-065's `<N.0` convention past the two packages that KI itself
  covered — a downstream `pip install ontolith`/any extra previously resolved whatever was newest
  for these seven at install time, unreviewed by this project. `pydantic`/`fastapi` in particular
  have a higher blast radius than either `strawberry-graphql`/`rdflib` (KI-065): `pydantic`
  underlies every domain model, `fastapi` sits on the same auth-bearing request path
  `strawberry-graphql`'s own `<1.0` bound was justified by. `fastapi`/`typer`/`uvicorn` are all
  long-lived pre-1.0 packages, same shape `strawberry-graphql` was in before KI-065's own floor
  bump — the `<1.0` bound guards against an eventual major release, not 0.x churn;
  `security.yml`'s weekly `pip-audit` remains the real backstop for that. `python-ulid`'s floor
  stayed at `2.0` (the lock resolves `3.1.0`) rather than being bumped to match, so its `<4.0`
  deliberately spans two majors instead of one — out of scope for this KI, which added upper
  bounds, not audited floors. The `dev` extra's ~20 tooling dependencies remain unbounded, also out
  of scope and lock-pinned in practice via the committed `uv.lock`. `uv lock` produced only an
  8-line lockfile metadata diff — no package's resolved version actually changed. ADR-0026 updated.
- MCP tools prefer an `Authorization: Bearer <token>` HTTP header over the `token` tool argument
  under the SSE/streamable-HTTP transports (closes KI-067, ADR-0014 update) — keeps a live
  credential out of the calling model's own context window and any MCP client's tool-call logging.
  `token` is now optional (`str | None = None`) on all 8 tools and remains the only channel on
  stdio, which has no HTTP request to carry a header on. A header that IS present but malformed
  (wrong scheme, blank value) fails the call closed rather than silently falling back to the
  argument.
- CI now runs `pip-audit`/`bandit`/`gitleaks`/SBOM generation (new `security.yml`, ADR-0026,
  closes KI-020) and a `griffe check` public-API breaking-change diff (informational pre-1.0, in
  `ci.yml`). Building the pass surfaced two real vulnerabilities, both fixed: `mcp` bumped to
  `>=1.28.1,<2.0` (PYSEC-2026-3483) and `sqlite-vec`'s pin bumped to `0.1.3` (PYSEC-2026-1938,
  `vec0` DELETE+INSERT workarounds from ADR-0020 re-verified against the new version).
- `cryptography` (a transitive dependency of `mcp` via `pyjwt[crypto]`, not directly declared)
  bumped 49.0.0 → 50.0.0 (`uv lock --upgrade-package cryptography`) to fix PYSEC-2026-3552 —
  disclosed after `security.yml`'s previous scheduled run, first caught failing `pip-audit` on
  `main` post-merge rather than on any feature PR's own diff. No `pyproject.toml` change (the
  version floor lives in the lockfile only). Also newly caught while `pip-audit` was blocking
  the same CI job from ever reaching its later steps: one genuine `bandit` B608 finding per
  backend on the `UNION ALL` relation-filter query `entities_where()` gained for KI-030 — same
  already-justified false-positive shape as the pre-existing vector-search `nosec`s (predicate/
  value are always parameter-bound; only a hardcoded-literal clause is interpolated), just never
  actually run locally against `bandit` until this pass. `security.yml`'s `bandit`/SBOM steps
  now run with `if: always()` so a `pip-audit` failure can no longer mask them again — the same
  masking is exactly how the two bandit findings went unseen across a full PR.
- `pip` (a transitive dependency of `pip-audit` itself, via `pip-api`) bumped 26.1.2 → 26.2.1
  (PYSEC-2026-3721/CVE-2026-13346) and `pymdown-extensions` (transitive via `mkdocs-material`/
  `mkdocstrings`) bumped 11.0 → 11.0.2 (PYSEC-2026-3654/CVE-2026-67422), both via `uv lock
  --upgrade-package` (closes KI-062, found in the M3 milestone-boundary security audit) — same
  lockfile-only shape as the `cryptography` bump above. Neither ships in the `ontolith` wheel or
  any runtime extra. No `pyproject.toml` change.
- `strawberry-graphql[fastapi]` and `rdflib` now carry an upper version bound (`<1.0`, `<8.0`
  respectively), matching `mcp`'s existing `<2.0` convention (closes KI-065) — previously
  unbounded, so a downstream `pip install ontolith[graphql]`/`ontolith[interop]` resolved
  whatever was newest at install time, unreviewed by this project. No version actually changed;
  both were already resolving within the new bounds. `<1.0` is a weaker guarantee for
  `strawberry-graphql` than `<2.0` is for `mcp`: it's still pre-1.0, and a *minor* release already
  broke this integration once (the `>=0.316` floor bump above), so the bound guards against the
  next major only, not the next 0.x break.
- **SQLite:** `assertion_event`/`proposal_event` now reject raw `UPDATE`/`DELETE`/`INSERT OR
  REPLACE` at the database layer via six triggers (closes KI-066, ADR-0041), making SPEC §17's
  "the audit trail MUST NOT be mutable" a store-level guarantee rather than a port-surface
  convention alone (previously, only `StorageBackend` exposing no update/delete method stood
  between the audit tables and any code holding the raw connection). **DuckDB gets no equivalent
  fix** — verified DuckDB (1.5.4) has no `CREATE TRIGGER` support and no connection-level access
  restriction to work around that; documented as a currently-unfixable backend asymmetry rather
  than left unaddressed. Two real bypasses found in two review rounds and closed before merge:
  (1) `INSERT OR REPLACE`'s implicit conflict-row delete doesn't fire a `BEFORE DELETE` trigger
  unless `PRAGMA recursive_triggers` is ON (SQLite defaults it OFF) — could otherwise silently
  rewrite an existing audit row, including its `actor` field; (2) that pragma is per-*connection*,
  not persisted in the database file, so a second raw connection to the same file revived the
  bypass regardless — closed durably with a third, schema-persisted `BEFORE INSERT ... WHEN
  EXISTS(...)` trigger per table, which needs no pragma at all.

### Security & Correctness Remediation (2026-07-06 – 2026-07-09)

A project audit (`deep-reviewer` + `security-reviewer`) found a chained CRITICAL
governance bypass — an untrusted AI agent could spoof a trusted owner via delegation,
force a `static` fact to be silently superseded instead of contradicted, and auto-accept
the resulting write — plus several HIGH/MEDIUM findings. All were fixed across five PRs;
a follow-up re-audit against the merged fixes then found two of the fixes had introduced
new HIGH regressions, closed in a sixth PR.

#### Fixed
- **CRITICAL:** conflict-routing temporality is now resolved from the active schema
  (`SchemaIR.temporality_of`), not accepted as a caller-supplied `propose()` argument —
  closes the bypass letting a `static` fact be silently superseded instead of raising a
  contradiction (SPEC §10)
- **CRITICAL:** MCP callers are now authenticated via per-principal API-key tokens
  (ADR-0014) — the acting principal is resolved from a verified bearer token, never a
  caller-asserted `author` string
- **HIGH:** delegation now uses `min(capability(author), capability(acting_as))` per
  SPEC §8.4 instead of substituting the delegating principal's full capability; an AI
  principal's own `kind` is checked before any capability math, so it can never reach
  `AutoAccept` by naming a trusted owner
- **HIGH:** direct writes (`assert_literal`/`assert_ref`) now route through SPEC §10
  conflict routing and require `write`/`admin` capability; AI-kind principals are
  hard-blocked from this path regardless of misconfigured capability (ADR-0003)
- **HIGH:** relations now have a governed proposal path (`propose_ref`), mirroring
  `propose()` for literals
- **HIGH:** `flag_contradiction` now enforces a capability gate (`>= propose`) — it was
  previously reachable by any principal, including read-only, with no check at all
- **HIGH:** `as_of()` now closes `valid_to` on retraction and excludes `flagged`
  assertions by default (`include_flagged=True` to opt in)
- **HIGH:** `retract()` no longer widens an already-closed `valid_to` — retracting an
  already-superseded assertion previously reopened its validity window, corrupting
  bitemporal reconstruction
- **HIGH:** `accept_proposal` now preserves `acting_as` (delegation provenance) when
  replaying a proposal's operations — it was previously dropped on the review-accept
  path, the primary path for AI-delegated proposals since AI proposals always require
  review
- **MEDIUM:** AI-authored assertions now require and capture `model` provenance
  (SPEC §7.4/§14.4); `propose()`/`propose_ref()` raise `ValidationError` for an AI
  author with no `model`
- **MEDIUM:** `accept_proposal` now re-resolves temporality from the current schema at
  apply time instead of trusting a snapshot taken at propose time, closing a narrow
  schema-migration side door back into the original temporality-spoofing issue
- **MEDIUM:** `issue_token`/`revoke_token`/`list_tokens` now require the calling
  principal to hold `admin` capability — previously ungated, so anything with backend
  access could mint a bearer credential for any principal
- **MEDIUM:** SQLite backend now enables WAL journal mode (SPEC §12.1 MUST)
- **MEDIUM:** an AI principal's `owner` must now resolve to an existing human/service
  principal (FOREIGN KEY constraint + application-layer check) rather than merely being
  a non-null string

#### Added
- Structured `proposal_event` log for accept/reject review actions (SPEC §9.4), so
  `policy_reason` (set by the policy engine at proposal-creation time) is no longer
  overwritten by the reviewer's free-text reason
- Structured, append-only `assertion_event` log covering every status mutation
  (supersession, flagging, retraction, contradiction-resolution reactivation), each
  independently attributable and timestamped
- `Contradiction.raised_by` — records the principal who raised each contradiction,
  whether auto-detected during conflict routing or explicitly flagged
- `StorageBackend.get_assertion(id)` — single-assertion lookup by ID, regardless of
  status
- ADR-0014: MCP authentication model (per-principal API-key tokens)
- `docs/known-issues.md` KI-014: plugin capability isolation is tracked as a required
  gate before plugin discovery/loading is ever enabled — no plugin loader exists yet to
  secure, so a sandbox was deliberately not built speculatively ahead of that need

#### Changed
- **Breaking:** `propose()` no longer accepts a `temporality` parameter — it is always
  resolved from the schema
- **Breaking:** `assert_literal`/`assert_ref` now require `write` or `admin` capability
  and reject AI-kind principals outright, even if misconfigured with elevated capability
- **Breaking:** MCP's `propose`/`flag_contradiction` tools take a bearer `token`
  parameter instead of a caller-supplied `author` ID

### M2 - Collaboration (0.2) (Complete)

#### Added
- Review workflow: `accept_proposal`/`reject_proposal` for `require_review` proposals
- Bitemporal time-travel via `Ontology.as_of(t)`
- SPEC §10 conflict routing: temporal supersession for `time_varying` properties,
  contradiction flagging for `static` properties
- `resolve_contradiction()` for reviewer-driven contradiction resolution (SPEC §10.3)
- MCP server exposing `schema`/`get`/`query`/`provenance`/`propose`/`flag_contradiction`
  tools, with no direct-write tool (ADR-0008), test-enforced
- Trust levels and `acting_as` delegation (ADR-0003)
- Hypothesis property tests for conflict routing and the append-only invariant

#### Fixed
- `Reject` decision is now actually produced by `ThresholdPolicy` for insufficient
  capability instead of silently falling through
- MCP `provenance`/`flag_contradiction` tools could not resolve non-active
  (retracted/superseded/flagged) assertions, defeating their primary audit-trail use case

#### Documented
- Confidence-based auto-accept for AI proposals is a deliberate design decision, not a
  gap: AI principals always require review regardless of confidence or trust level

### M1 - Substrate (0.1 MVP) (Complete)

#### Added
- Meta-model + IR and the class-based schema DSL (first front-end; LinkML YAML followed
  in M3)
- SQLite storage backend (default adapter) with append-only entity/assertion tables
- Append-only `Assertion` model with full provenance (author, source, confidence,
  rationale, model, bitemporal fields)
- Identity basics: principals (`human`/`ai`/`service`), capability levels
  (`read < propose < write < review < admin`), AI accountable-owner requirement
- `propose()` → `ThresholdPolicy` evaluation → auto-accept/require-review/reject
- Basic query builder, Python SDK, CLI

### M0 - Foundations (Complete)

#### Added
- Repository structure and build configuration
- Core ports: Clock and IdProvider for deterministic behavior
- Error taxonomy with stable error codes
- Architecture Decision Records (ADR-0001 through ADR-0008)
- GitHub Actions CI workflow
- Contribution guidelines and community health files
- import-linter configuration for dependency rule enforcement

[Unreleased]: https://github.com/mattbv/ontolith/compare/v0.0.1...HEAD
