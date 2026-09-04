# ADR-0044: Observability (SPEC §18) Is Scoped to M4, Not Built Opportunistically

**Status**: Accepted

**Date**: 2026-09-04

**Deciders**: Ontolith Core Team

**Related**: SPEC §18 (metrics, events, structured logs — SHOULD, not MUST), SPEC §19 (conformance —
§18 names no conformance vector), `docs/Ontolith_Implementation_Plan.md` §2 (milestone table),
ADR-0001 (Clock/IdProvider ports — the precedent this ADR follows for keeping I/O out of pure
domain code), `docs/known-issues.md` KI-064

---

## Context

`src/ontolith/observe/__init__.py` has been an empty package since M0's repository skeleton. SPEC
§18 asks implementations to emit three things: **metrics** (proposal counts, auto-accept rate,
review latency, open contradictions, human-vs-AI ratio, query latency, vector index size),
**events** (proposal lifecycle, contradiction open/resolve, schema migration, plugin load), and
**logs** (structured, correlated by `namespace`/`principal`/`acting_as`/`proposal_id`). None of
this exists today. What exists instead is ad hoc, uncorrelated `logging.getLogger(__name__)` calls
in four modules (`plugins/registry.py`, `interfaces/{rest,graphql,mcp}.py`) — none of them
structured, none of them carrying the correlation fields SPEC §18 names, and none of them backed
by a metrics or event sink of any kind.

This was flagged mid-M3 as a lower-confidence, informational note — SPEC §18 uses SHOULD, not
MUST, and names no conformance vector (SPEC §19), so it never blocked a milestone gate. It stayed
untracked as a real decision through M0–M3 for the same reason: nothing forced the question. At
the M3 boundary that stopped being a good reason to keep deferring it — REST, GraphQL, MCP, and
CLI are all now shippable, a production-shaped deployment is genuinely possible, and the
Implementation Plan's own M4 milestone-scope column (`Ontolith_Implementation_Plan.md` §2) never
named observability at all — an oversight, not a decision, since M4 is literally titled "Production
(1.0)" and SPEC §18 exists precisely for production operability.

KI-064 asked for one of two outcomes: record an ADR scoping M4's observability plan, or explicitly
decide it's out of scope through 1.0. This ADR takes the first path.

## Decision

**Observability (SPEC §18) is in scope for M4, not before, and not silently dropped from 1.0.**
Concretely:

**1. M4's Implementation Plan scope column is updated** (this ADR's companion edit) to name SPEC
§18 explicitly, so the gap this ADR exists to close doesn't quietly reappear at the next milestone
boundary the way it did at M0–M3's.

**2. No implementation lands as part of this ADR.** This is a scoping decision, not a feature PR —
consistent with KI-064's own "at minimum, record an ADR" framing and with the Implementation Plan's
own rule that a milestone's scope isn't started opportunistically ahead of its own turn (M4 "Not
started" as of this ADR). The plan below is what M4 implements, not what this PR implements.

**3. The architecture, decided now so M4 doesn't have to re-litigate it:**

- **A single abstract port, not three.** SPEC §18's metrics/events/logs are one concern
  (observability) with three shapes of output, not three independent subsystems. One
  `ObservabilitySink` protocol (or similarly named port, final name decided at implementation time)
  lives beside `Clock`/`IdProvider` in `core/`'s port definitions, with methods shaped roughly like
  `record_metric(name, value, **tags)`, `record_event(kind, **fields)`, `log(level, msg, **fields)`.
  A default no-op implementation ships so instrumentation calls are always safe to make even when
  no real sink is configured — mirroring `Clock`'s own "always injected, never optional at the call
  site" shape, not an `if observer:` check scattered at every call site.
- **Dependency rule holds.** `core/`, `schema/`, `govern/`, `query/` depend only on the abstract
  port, exactly as they already do for `Clock`/`IdProvider`/`StorageBackend`. Concrete sinks
  (stdlib `logging`-backed, OpenTelemetry-backed, a test-double recording sink, etc.) live in
  `observe/` as adapters and are wired at the composition root — the same place `StorageBackend`
  and `Clock` implementations are chosen today.
- **`govern/policy` stays pure.** Policy evaluation itself never calls the sink — SPEC's own
  purity requirement for the policy engine doesn't get a carve-out for observability. Instrumentation
  happens one layer up, at the proposal-acceptance orchestration in `Ontology` (mirroring where
  `AdminEvent` recording already happens, ADR-0042) and in `interfaces/*` for request-level logging
  (query latency, auth outcomes) — not inside the pure decision function itself.
- **Determinism is not a requirement for observability output**, unlike `Clock`/`IdProvider`.
  Timestamps and correlation ids used *inside* metrics/events/logs should still come from the
  injected `Clock`/`IdProvider` (never `datetime.now()`/`uuid4()` directly, per the project's
  existing determinism rule) so tests can assert on emitted observability data deterministically,
  but the sink's own side effects (writing to a log stream, incrementing a counter) are I/O by
  nature and sit behind the port precisely so domain tests never depend on them.
- **Correlation fields are carried, not re-derived.** `namespace`, `principal`, `acting_as`, and
  `proposal_id` already flow through `Ontology`'s proposal-acceptance path and through every
  interface's request handling today — instrumentation threads them through as call arguments to
  the sink, it does not need a new context-propagation mechanism (no `contextvars`, no thread-local
  state) to get them.
- **Minimum viable M4 scope, in priority order:** (a) structured, correlated logs replacing the
  four existing ad hoc `logging.getLogger()` call sites, since these already exist in a
  lower-value form and SPEC §18 calls logs out specifically for correlation; (b) the four named
  events (proposal lifecycle, contradiction open/resolve, schema migration, plugin load), each of
  which already has exactly one call site to instrument (mirroring how ADR-0042's `AdminEvent`
  found one call site per action); (c) the metrics list, which is the largest surface (8 named
  metrics, several needing new counters/gauges that don't exist as tracked values anywhere today)
  and the one most likely to need its own follow-up ADR for a concrete backend choice
  (OpenTelemetry vs. a minimal counts-in-SQLite approach vs. Prometheus client library) once M4
  actually starts.

**4. Still SHOULD, not MUST — this does not become an M4 exit-gate criterion.** M4's exit criteria
(security review, performance budgets, SemVer 1.0 freeze, `format_version` freeze) are unchanged by
this ADR. Observability is *scoped into M4's work*, not promoted to a *blocking gate* — SPEC itself
only ever asked for SHOULD, and this ADR has no basis to raise that bar unilaterally. A milestone
can ship its scope column incomplete if a genuine tradeoff emerges once M4 is actually underway;
this ADR's job is only to make sure that's a considered decision made at M4 time, not a default
that happens by nobody having planned for it.

## Consequences

**Positive:**
- Closes KI-064: the gap now has a plan and a milestone home, instead of being re-discovered and
  re-deferred at each milestone boundary.
- The Implementation Plan's M4 scope column now names observability explicitly — the next
  milestone-boundary audit has something concrete to check against, closing the same "oversight,
  not decision" gap this ADR itself was written to fix.
- The port-based architecture decided here reuses the exact `Clock`/`IdProvider` precedent
  (ADR-0001) rather than inventing a new pattern, keeping the dependency rule's enforcement
  (`import-linter`) mechanical rather than requiring a new contract.

**Negative / follow-ups:**
- `observe/` remains an empty package until M4 actually starts — this ADR plans the work, it does
  not do it. The production-deployment gap named in KI-064's own Description (no way to observe
  proposal-acceptance rate, review latency, etc., today) persists through M3's close and however
  long M4 takes to begin.
- The metrics surface (SPEC §18's 8 named metrics) is deliberately left for a follow-up ADR at M4
  implementation time rather than fully designed here — picking a concrete metrics backend is a
  real architectural decision (dependency footprint, whether it's pluggable like `StorageBackend`)
  better made against M4's actual requirements than speculatively now.
- Because this ADR intentionally does not implement anything, `docs/known-issues.md`'s KI-064 is
  resolved as "planned," not "built" — a distinction worth being explicit about rather than letting
  "✓ RESOLVED" read as "the observability gap is gone."

## Alternatives Considered

- **Implement a minimal version now (e.g., just the structured-logging piece) rather than only
  writing an ADR:** rejected for this PR — would be scope creep ahead of M4's own turn (M4 "Not
  started"), the same milestone-discipline CLAUDE.md's own Definition of Done names ("does not
  introduce scope creep beyond milestone goals"). Structured logging is deliberately named first in
  this ADR's priority order specifically so it's the obvious starting point once M4 begins, not
  lost among the larger metrics/events surface.
- **Explicitly declare observability out of scope through 1.0:** rejected — SPEC §18 exists for
  exactly the production-readiness need M4 itself represents ("Production (1.0)"); declaring it
  out of scope through the 1.0 milestone that is *named* production would need a stronger
  justification than "it's only a SHOULD." Nothing in the KI's own Description argues the feature
  isn't wanted, only that it had drifted into being silently deferred.
- **Three separate ports (`MetricsSink`, `EventSink`, `Logger`) instead of one `ObservabilitySink`:**
  rejected — SPEC §18 groups these as one concern with three output shapes, and every concrete
  adapter (stdlib logging, OpenTelemetry) that would plausibly implement this is also one
  integration spanning all three, not three independent ones. A single port keeps the composition
  root (and `import-linter`'s contract surface) simpler; nothing observed so far suggests a
  deployment that would want, say, metrics from one backend and logs from an unrelated one.
- **A `contextvars`-based correlation-context mechanism (implicit propagation) instead of explicit
  call arguments:** rejected — `namespace`/`principal`/`acting_as`/`proposal_id` are already
  explicit parameters or attributes at every call site that would need to log them; implicit
  thread-local propagation would add a new, harder-to-test mechanism to carry data that's already
  sitting in scope.

## References

- SPEC §18: Observability
- SPEC §19: Conformance (names no vector for §18 — confirms SHOULD, not MUST)
- `docs/Ontolith_Implementation_Plan.md` §2: Phasing & sequencing (M4 scope column, updated
  alongside this ADR)
- ADR-0001: Clock & IdProvider ports (the precedent this ADR's port design follows)
- ADR-0042: Admin-Action Audit Trail (the closest existing precedent for "one call site per
  domain action" event recording, reused here for the proposal-lifecycle/contradiction/schema/
  plugin events)
- `docs/known-issues.md` KI-064 (resolved by this ADR)
