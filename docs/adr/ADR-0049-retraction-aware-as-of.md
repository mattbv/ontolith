# ADR-0049: `as_of()` Excludes a Retraction Once Its Own Assertion-Time Has Passed

**Status**: Accepted

**Date**: 2026-09-13

**Deciders**: Ontolith Core Team

**Related**: SPEC §11.2 ("Flagged/superseded/retracted assertions are excluded by default"), SPEC
§11.4 (`as_of` semantics), SPEC §10 (retraction), `docs/known-issues.md` KI-095 (closed by this
ADR), ADR-0048 (`.include_flagged()`/`.include_history()` wideners — this ADR makes
`.include_history()` finally meaningful under `.as_of()`, previously a documented no-op there)

---

## Context

`kb.as_of(t)` reconstructs "what did we believe was true at time `t`" by matching an assertion
whose validity window covers `t` (`valid_from <= t < valid_to`) and whose own `asserted_at <= t`.
This works correctly for **supersession**: when a `time_varying` value changes, the old
assertion's `valid_to` is closed to the new assertion's `valid_from` (`supersede()`,
`bitemporal.md`), so the old assertion's window itself already tells the truth about when it
stopped being valid — `as_of(t)` naturally excludes it once `t` passes that point, no extra logic
needed.

**Retraction has no such self-describing window.** `Ontology._retraction_valid_to()` narrows an
open-ended (`valid_to IS NULL`) window to "now", but deliberately leaves an already-set `valid_to`
untouched — including one the original caller supplied explicitly (`assert_literal(...,
valid_to=...)`), not just one closed by a prior status change. So an assertion asserted with
`valid_to=2030` and retracted in 2025 keeps `valid_to=2030` in the row. `as_of(t)`'s validity-window
check has no idea a retraction ever happened — it just sees a window that still covers, say,
`t=2026`, and returns the entity. `.include_history()` (ADR-0048) is a documented no-op on this
path for exactly this reason: the `as_of` branch never restricted matches to `active` in the first
place, so there was nothing for the widener to add, but also — the flip side no one had built —
nothing to let a caller *exclude* the retraction judged unwanted.

Verified: assertion with `valid_to=2030`, retracted in 2025 → current-state query correctly
excludes it (`status='active'` filter) → `as_of(2026)` query wrongly includes it → SPEC §11.2's
"excluded by default" MUST is unmet on this one path.

The system already timestamps the retraction event itself: `_record_assertion_event(assertion_id,
author, "retracted", now)` writes one `assertion_event` row per retraction (exactly one, ever — a
second `retract()` call on an already-`retracted` assertion is a verified no-op, KI-051, so there
is never more than one `retracted` event per `assertion_id` to disambiguate). That timestamp is
never consulted by any bitemporal query path today.

## Decision

**`as_of(t)` treats a `retracted` assertion's own retraction event as a second, independent
assertion-time cutoff, on top of the existing valid-time window check.** An assertion is visible
under `as_of(t)` iff (in addition to the existing valid-time and `asserted_at` checks): its status
is not `retracted`, OR — if it is — the `assertion_event` row recording that retraction has
`at > t` (the retraction had not yet happened, assertion-time-wise, as of `t`).

Concretely, `entities_where()`'s (and `entities_meeting_confidence()`/`entities_meeting_trust()`'s
— KI-093 established these three move together) `as_of_time` branch gains, alongside the existing
`flagged_clause`:

`StorageBackend.assertions()` — a distinct, lower-level bitemporal query path this ADR's own KI
text named alongside `entities_where()` ("and the other bitemporal query paths") — gets the
identical clause, unconditionally (it has no `include_history`-shaped opt-out today, unlike the
`QueryBuilder`-facing trio, since its `status` parameter is already ignored entirely once
`as_of_time` is set). This matters beyond direct callers: `govern/policy.py`'s `SourceQuorum`
evaluates `kb_view.assertions(...)` on an `AsOfView`, so an un-narrowed retracted assertion could
silently contribute a distinct `source` to quorum counting during policy evaluation — a governance
correctness issue, not just a read-path one.

```sql
AND (status != 'retracted' OR EXISTS (
    SELECT 1 FROM assertion_event ae
    WHERE ae.assertion_id = assertion.id
      AND ae.action = 'retracted'
      AND ae.at > ?   -- bound to as_of_time
))
```

**`.include_history()` becomes the opt-out**, exactly mirroring its existing current-state meaning
("also match `retracted`/`superseded` assertions"): when set, this new clause is skipped entirely
and `as_of(t)` reverts to the pre-ADR window-only behavior. This is the first thing
`.include_history()` has ever done on the `as_of` path — every prior KI-081/093/094/096 note
calling it "a documented no-op under `.as_of()`" is now scoped to say "unless retraction is
involved" (their sibling claim, "`.include_flagged()` still applies under `.as_of()`", is
unaffected and remains exactly as before).

No new column, no schema migration, no data loss: the timestamp this decision needs
(`assertion_event.at` for the `retracted` action) already exists and is already written on every
retraction. `valid_to` itself is never touched by this ADR — an assertion originally asserted with
an explicit real-world end date keeps that date fully intact and queryable after retraction; only
`as_of` *visibility* changes.

## Rationale

**Two independent bitemporal axes, kept independent.** Valid-time (`valid_from`/`valid_to`) answers
"when was this true in the real world"; assertion-time (`asserted_at`, and now the retraction
event's `at`) answers "when did the system believe/stand behind this claim." A retraction is a
correction to belief, not new information about the real world's own timeline — it says nothing
about when the fact stopped being true (that's what `valid_to` is for, and it may be a date the
retracting principal never revisited or agrees with). Conflating the two by force-closing
`valid_to` to the retraction instant (the rejected alternative below) would silently discard
correct valid-time information to encode an assertion-time fact.

**No information is destroyed.** The `retracted` `AssertionEvent`'s `at` is already the correct,
already-persisted answer to "when, in assertion-time, did we stop standing behind this" — this ADR
is entirely about *reading* an existing fact that no query path previously consulted, not about
capturing something new.

**Symmetric with the existing `asserted_at <= t` check.** An assertion becomes visible starting
exactly at its own `asserted_at` (not strictly after); this ADR makes it become invisible starting
exactly at its own retraction event's `at` (not strictly after either) — `at > t` is the correct
inclusive-exclusive boundary matching that existing convention, not an arbitrary choice.

**Supersession needs no equivalent fix.** Its `valid_to` closure already tells the truth about the
real-world end point (the successor's own `valid_from`), so `as_of(t)`'s ordinary window check
already handles it correctly — this ADR's clause is deliberately scoped to `status = 'retracted'`
only.

## Consequences

- **Behavior change, not a signature change:** `kb.as_of(t).query(...)` results, and
  `kb.as_of(t).assertions(...)` results, for `t` after a retraction's own assertion-time can now
  exclude an entity/assertion they previously included. No public method signature changes;
  `StorageBackend.entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()` keep
  their existing parameters (`include_flagged`, `include_history`) — only what `include_history`
  *does* under `as_of` changes, from "nothing" to "opts back into the pre-ADR behavior."
  `assertions()` gains no new parameter at all: the exclusion is unconditional there, since that
  method has no equivalent opt-out today (a caller wanting a retracted assertion back under
  `as_of` has no way to ask, on this method specifically — a known, accepted asymmetry, not
  something this ADR adds a parameter to close).
- **A new correlated `EXISTS` subquery on the `as_of` path** in all four methods, scoped by the
  existing `idx_assertion_event_assertion` index (on `assertion_event.assertion_id`) — only
  evaluated per candidate row already matching every other `as_of` predicate. Whether SQLite/DuckDB
  actually short-circuit the `OR` on the cheaper `status != 'retracted'` check first is a query-planner
  detail neither engine documents as guaranteed — expected, not proven, and not load-bearing for
  correctness either way.
- **`.include_history()`'s docstring, `entities_where()`'s/`entities_meeting_confidence()`'s/
  `entities_meeting_trust()`'s/`assertions()`'s docstrings, `QueryBuilder.min_confidence()`'s and
  `.trust_at_least()`'s docstrings, and every "documented no-op under `as_of`" claim from
  KI-081/093/094/096 (including ADR-0048's own body, not just its cross-reference index) needed
  updating** to the narrower, now-accurate claim.
- **This closes KI-095's SPEC §11.2 compliance gap** on every `as_of`-capable read path this
  session could find (`entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()`
  via `QueryBuilder`, and `assertions()` directly) without touching `_retraction_valid_to()` or the
  write-time retraction path at all — the fix is entirely read-side. **Filed separately, not
  closed here:** KI-097 (`flagged`/`reactivated` reconstructed from current status rather than
  point-in-time on the same three `QueryBuilder`-facing methods — `assertions()` already handles
  that pair correctly via its own pre-existing event-log reconstruction, which this ADR's fix
  mirrors for the one-way `retracted` case).
- **Fail-closed on a missing event:** an assertion with `status = 'retracted'` but no matching
  `assertion_event` row (reachable only via a direct `put_assertion()` at the port level, or a
  pre-existing database written before this fix shipped — no migration was needed, since
  `assertion_event` already existed) is invisible under `as_of` at *every* `t`, not just after the
  (unknown) retraction. This is the safer of the two possible defaults and is deliberate, not an
  oversight — called out here since neither the code comments nor an earlier draft of this ADR
  mentioned it.
- **"Latest retraction wins" is untested territory the code doesn't need to handle**: KI-051
  guarantees at most one `retracted` event per assertion (retraction is a one-way terminal
  transition — verified against all four call sites that set `status = 'retracted'`), so the
  `EXISTS (... AND ae.at > ?)` clause never has more than one candidate row to find. If that
  invariant were ever relaxed, this clause's semantics would become "excluded once *any* retraction
  event is known" regardless of ordering — worth a comment at the invariant's own definition site if
  it ever changes, not a defense added speculatively here.

## Alternatives Considered

**Close `valid_to` to the retraction instant at write time** (`_retraction_valid_to()` always
narrows to `now`, even over an already-explicit future `valid_to`). Rejected — conflates
assertion-time correction with valid-time fact, and is a genuine, permanent data loss: the
assertion row is mutated in place (append-only invariant explicitly permits `valid_to` to mutate,
bitemporal.md #1), and `AssertionEvent` records only `action`/`actor`/`at`, never a snapshot of the
fields it changed — so an originally-asserted `valid_to=2030` would be unrecoverable the instant a
retraction narrowed it, with no other table retaining it. Simpler (one function, no new query
logic, no schema/index consideration) but at a real, irreversible information cost this ADR's
approach avoids entirely for free (the needed timestamp already exists elsewhere).

**Add a dedicated `assertion.retracted_at` column** instead of querying `assertion_event`. Rejected
as unnecessary duplication — `assertion_event.at` for the `retracted` action is already the single
source of truth for this timestamp (KI-051 guarantees exactly one such row per assertion), and a
new column would need to be kept in sync with it on every write path that retracts, reintroducing
exactly the kind of two-copies-of-one-fact drift this codebase's audit-trail design already avoids
elsewhere (assertion rows carry only current state; `assertion_event` is the append-only history).

**Leave `.include_history()` a no-op under `as_of` and add a separate new opt-out parameter.**
Rejected — `.include_history()` already means "also match non-`active` statuses"; a caller who
wants to see a retracted assertion under `as_of` despite this fix is asking for exactly that, and
giving it a second name would be a needless third way to say the same thing SPEC §11.2 already
names once.
