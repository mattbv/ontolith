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

#### Fixed
- **HIGH:** `as_of(t)` excluded flagged assertions by current status instead of
  status-at-t; since flagging never sets `valid_to`, once any contradiction had ever
  touched a `(subject, predicate)`, `as_of(t)` returned nothing for it at any t, including
  times before the dispute existed. Fixed by recording a `flagged` event for the newly
  incoming assertion in a fresh contradiction (previously only the pre-existing member
  got one) and reconstructing flagged-status-at-t from `assertion_event` instead of
  trusting current status
- **HIGH:** `accept_proposal`/`reject_proposal`/`resolve_contradiction` now reject a
  reviewer who is the proposal's own author or delegate (`acting_as`) — self-review,
  including via delegation chain, was previously possible for a misconfigured principal
  with review capability
- `ontolith.provenance` (MCP) and `flag_contradiction` fetched and deserialized every
  assertion in the KB to find one or two rows by ID; both now use the indexed
  `get_assertion(id)` lookup
- `assertions()`'s composite index led with `namespace`, which no query filters on
  (single-namespace today), making it unusable — confirmed via `EXPLAIN QUERY PLAN` (full
  `SCAN`, not `SEARCH`). Added indexes matching the actual filter shapes in both backends

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
