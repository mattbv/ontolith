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

#### Fixed
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

#### Security

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
