# ADR-0023: Recovering the Full Predecessor Set for Multi-Target Supersession

**Status:** Accepted

**Date:** 2026-07-23

**Deciders:** Ontolith Core Team

**Related:** SPEC §10.2 (temporal supersession), SPEC §12.2 (normative SQLite
schema shape), ADR-0019 (public API stability policy)

## Context

KI-008 documented a known v1 fidelity gap: `Assertion.supersedes` is a
scalar `str | None` field. When one incoming `time_varying` assertion
supersedes several *concurrently overlapping* predecessors at once — e.g. a
data-entry race left two employers both "active" simultaneously, and a third
value arrives that overlaps both — `Ontology._apply_with_conflict_routing`
only recorded the first predecessor on the new assertion's `supersedes`
field. `govern/conflict.route()` already computed the full predecessor set
correctly (`Supersede.targets: list[str]`); the loss happened one layer up,
where only `targets[0]` was written into the successor's `supersedes` field.

The obvious fix — widen `Assertion.supersedes` to `list[str]` — was rejected.
SPEC §12.2's normative SQLite schema models `supersedes` as a scalar `TEXT`
column, and `Assertion` is a `frozen=True` pydantic model whose shape is part
of the public API surface (ADR-0019). Widening it would be a SPEC-schema
deviation and a breaking change to a heavily-used public model, to fix a
fidelity gap in what is explicitly documented as a rare data-entry race, not
a routine path.

## Decision

**1. Keep `Assertion.supersedes` scalar, unchanged.** It still records the
first predecessor exactly as before (`result.targets[0]`) — no behavior
change for any existing caller.

**2. Recover the full predecessor set through the existing
`assertion_event` audit log instead of a new table.** Every predecessor
`_apply_with_conflict_routing` supersedes already gets its own
`assertion_event` row (`action="superseded"`) — that per-predecessor
granularity already existed, it just didn't record *which* successor caused
the transition. Added a `successor_id: str | None = None` field to
`AssertionEvent` (`core/assertion.py`), populated only for `superseded`
events, and a matching `successor_id` column (+ index) on both backends'
`assertion_event` table. A new `StorageBackend.get_assertion_events_by_
successor(successor_id)` port method queries by that column; the full
predecessor set is `{e.assertion_id for e in events}`.

This reuses an existing, already-correctly-timed write path (the loop in
`_apply_with_conflict_routing`'s `Supersede` branch already iterates every
target) rather than introducing a new table, new write path, or new public
model — the smallest change that closes the actual gap (full-fidelity
lookup), as opposed to changing what every caller of `Assertion.supersedes`
sees.

**3. `_apply_with_conflict_routing`'s write order changed: the successor
assertion is now persisted (`put_assertion`) before the loop that closes and
records events for its predecessors**, not after. SQLite's new
`FOREIGN KEY(successor_id) REFERENCES assertion(id)` constraint requires the
referenced row to exist at insert time, and both inserts happen inside the
same transaction this method already documents as required — reordering
within one transaction doesn't change atomicity or the append-only
invariant, only the sequence of statements inside it. DuckDB's
`assertion_event` table has no such FK (matching its existing, already-
commented precedent for `assertion_id` — a documented DuckDB 1.5.4
constraint-checking quirk on same-transaction child-then-parent-update
sequences, referential integrity enforced at the application layer instead).

**4. Exposed via both read surfaces that already serve provenance data**:
REST `GET /provenance/{id}` gained `ProvenanceOut.superseded_ids: list[str]`
and the MCP `ontolith.provenance` tool gained a matching `superseded_ids`
key — both computed the same way, calling the new backend method directly
(the existing pattern both routes already use for `get_proposal_events`).
No new `Ontology`-level wrapper method was added; `kb.backend` is already
directly reachable for this kind of audit-trail read, matching how these two
routes already call backend methods for provenance enrichment.

**5. The new `get_assertion_events_by_successor` method sorts `ORDER BY at
ASC, id ASC`; the pre-existing `get_assertion_events` method keeps its
original `ORDER BY at ASC` unchanged.** The two methods are not held to the
same bar: `get_assertion_events_by_successor` has no prior callers or prior
ordering contract to preserve, so adding an `id ASC` tiebreak for
determinism under a coarse/injected `Clock` is free. Retrofitting the same
tiebreak onto `get_assertion_events` was tried and reverted during review —
that method's existing callers and tests (e.g.
`conformance/test_assertion_audit_log.py`) implicitly rely on insertion-order
(rowid) tiebreaking, which happens to match causal order; switching to
lexical `id ASC` would silently invert that order once an id crosses a
digit-length boundary (`"id-10" < "id-9"` lexically), a regression risk with
no corresponding benefit since the method's only real ambiguity — same-`at`
events — is rare and, where it matters (KI-008's own recovery path), is now
resolved by querying `get_assertion_events_by_successor` instead.

## Rationale

**Why not a dedicated `supersession_link` table** (the alternative KI-008's
own text named): would still be a new table, new write path, and new port
methods — no smaller than the audit-log route, and duplicates data
`assertion_event` already almost captures (one row per predecessor, at the
right moment, missing only the successor pointer). Reusing it is strictly
less new surface for the same fidelity guarantee.

**Why the write-order change is safe:** no uniqueness or business-logic
invariant depends on predecessors being closed before the successor exists —
assertion IDs are already unique and there is no DB-level constraint tying
"at most one active assertion per (subject, predicate)" (that invariant is
enforced by conflict routing itself, at the application layer, not by a
schema constraint). A reader querying mid-transaction never observes partial
state regardless of order, since `transaction()` guarantees atomic
visibility.

## Alternatives Considered

**Widen `Assertion.supersedes` to `list[str]`, rejected.** Breaks the SPEC
§12.2 normative schema shape and the public `Assertion` model's field type
(ADR-0019) to fix a rare-path fidelity gap — disproportionate for what KI-008
itself describes as "rare... a data-entry race."

**Add a new `supersession_link(successor_id, predecessor_id)` table,
rejected.** Strictly more new surface than extending `assertion_event`,
which already writes one row per predecessor at exactly the right point in
the same code path.

**Do nothing (leave KI-008 open as accepted v1 debt), rejected.** The fix
turned out small and low-risk once framed as an audit-log extension rather
than a model change — no reason to leave a known, closeable correctness gap
open once a non-breaking path was found.

## Consequences

**Positive:** The full predecessor set for any multi-target supersession is
now recoverable via `StorageBackend.get_assertion_events_by_successor()` and
surfaced in both provenance read paths, with no breaking change to
`Assertion`, no SPEC-schema deviation, and no new public `Ontology` method.

**Negative / follow-ups:** `Assertion.supersedes` itself still only names one
predecessor — a caller reading `supersedes` alone (not the event log) still
sees only the first. This is unchanged from before and is documented at the
field level; KI-008 is closed on the basis that full fidelity is now
*recoverable*, not that the scalar field itself became a list.

## Update (2026-07-24): review-driven fixes

Review of this slice before merge found two HIGH, one MEDIUM, and two LOW
issues, all addressed in the same PR:

- **`get_assertion_events`'s ordering was retrofitted with an `id ASC`
  tiebreak it shouldn't have inherited** — see the revised Decision §5 above.
  Reverted to its original `ORDER BY at ASC`; the `id ASC` tiebreak stays
  only on the new `get_assertion_events_by_successor` method, which has no
  prior ordering contract to break.
- **`docs/known-issues.md`'s KI-008 entry still prescribed the rejected
  `list[str]` widening** in its own "Fix" text, left unedited while the
  actually-implemented design (this ADR) took a different path. Fixed by
  marking KI-008 resolved with a corrected Fix section pointing at
  `successor_id`/`get_assertion_events_by_successor`.
- **`CHANGELOG.md` had no entry for either change** — the new
  `StorageBackend.get_assertion_events_by_successor` Protocol method is a
  breaking change for third-party backend implementers per ADR-0019 (any
  external `StorageBackend` implementation is now missing a required
  method) and needed a `**Breaking:**` marker; `AssertionEvent.successor_id`
  is additive and needed a normal entry. Both added.
- **DuckDB's `successor_id` column lacked the explanatory comment** its
  sibling `assertion_id` column already carries, explaining why no
  `FOREIGN KEY` is used (same DuckDB 1.5.4 mutate-after-referenced quirk).
  Added, mirroring the existing comment.

## References

- SPEC §10.2 (temporal supersession), §12.2 (normative SQLite schema)
- `docs/known-issues.md` KI-008 (now resolved)
- ADR-0019 (public API stability policy — why `Assertion`'s shape wasn't
  changed), ADR-0022 (credential-ordering `id ASC` tiebreak precedent)
