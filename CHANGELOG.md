# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### M3 - Extensible (0.3) (In Progress)

#### Added
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

#### Fixed
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

#### Security

- CI now runs `pip-audit`/`bandit`/`gitleaks`/SBOM generation (new `security.yml`, ADR-0026,
  closes KI-020) and a `griffe check` public-API breaking-change diff (informational pre-1.0, in
  `ci.yml`). Building the pass surfaced two real vulnerabilities, both fixed: `mcp` bumped to
  `>=1.28.1,<2.0` (PYSEC-2026-3483) and `sqlite-vec`'s pin bumped to `0.1.3` (PYSEC-2026-1938,
  `vec0` DELETE+INSERT workarounds from ADR-0020 re-verified against the new version).

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
