# ADR-0035: `flag_contradiction()` Requires at Least One Eligible Winner Among a *New* Contradiction's Founding Members

**Status**: Accepted
**Date**: 2026-08-26
**Deciders**: Ontolith Core Team
**Related**: SPEC §10.3 (contradiction resolution), SPEC §14 (MCP `ontolith.flag_contradiction`), ADR-0031 (retraction/supersession are terminal for `resolve_contradiction()` winner selection), KI-034 (conflict-routing resurrection guard), KI-044 (`resolve_contradiction()` terminal-winner rejection), KI-045 (`flag_contradiction()` TOCTOU fix), KI-050

---

## Context

`flag_contradiction(assertion_id_a, assertion_id_b, author)` validates that the two named assertions share a subject and predicate, then either opens a brand-new `Contradiction` naming them as its two founding members, or extends an already-open one for that `(subject, predicate)` if one exists. Since ADR-0031, it deliberately accepts either (or both) of the two assertions already being `retracted`/`superseded` — that's by design, so a terminal assertion can still be named when *extending* an open contradiction for audit/context.

But nothing previously stopped both assertions passed at **creation** time from already being terminal: `flag_contradiction(retracted_id, superseded_id, author)` opened a brand-new `Contradiction` whose two founding `member_ids` were both ineligible winners under ADR-0031's own check — `resolve_contradiction()` would fail against every existing member immediately, since it rejects a `retracted`/`superseded` winner candidate (KI-044). The contradiction wasn't permanently stuck (a third write on the same `(subject, predicate)` — a fresh `propose()`/`assert_literal` for a `static` predicate, or another `flag_contradiction()` call for `time_varying`, per ADR-0031's own escape hatch — reaches it exactly as ADR-0031 describes for any all-terminal contradiction), but it forced a caller who didn't ask for that outcome to know to work around it, at only `propose` capability, including by an AI principal over MCP (`ontolith.flag_contradiction` carries no write capability at all).

Found during KI-044/ADR-0031's own second review pass, confirmed and scoped precisely across two further review passes on this KI's own tracking entry (which initially overclaimed the contradiction was permanently unresolvable, corrected before this ADR).

## Decision

`flag_contradiction()` now rejects opening a **new** contradiction whose two founding members are *both* already terminal (`retracted` or `superseded`), raising `ValidationError` — mirroring `resolve_contradiction()`'s own winner-eligibility check (KI-044, ADR-0031) both in mechanism (a `ValidationError` for a structurally invalid write, not a capability-based `CapabilityError`/route-to-review) and in the specific terminal-status set checked.

**This only guards the "create" branch.** Extending an *already-open* contradiction with an all-terminal pair remains permitted — that's the case ADR-0031's own design deliberately supports (naming a terminal assertion for audit/context when extending), and this ADR does not touch it. The check fires only when `get_open_contradiction(namespace, subject, predicate)` finds no existing open contradiction to extend — i.e. only in the branch that would otherwise call `put_contradiction()` to create a new one.

```python
else:
    if a.status in ("retracted", "superseded") and b.status in ("retracted", "superseded"):
        raise ValidationError(
            f"Cannot open a new contradiction between {assertion_id_a!r} "
            f"(status={a.status!r}) and {assertion_id_b!r} (status={b.status!r}): "
            "both are already terminal, leaving no eligible winner for "
            "resolve_contradiction() to select"
        )
    contradiction_id = self.id_provider.next()
    ...
```

The check is placed *before* `self.id_provider.next()` is called, so a rejected call doesn't consume an ID for a contradiction that's never persisted.

## Rationale

**Why reject outright (`ValidationError`), not route to review** — unlike KI-043's capability-floor gap, which routes a below-floor caller to review rather than rejecting (to avoid a capability inversion where a higher-capability principal ends up worse off than a lower-capability one): this isn't a capability shortfall, it's a structurally invalid request regardless of who makes it. There's no "review" outcome that fixes two already-terminal assertion IDs into eligible ones — a reviewer accepting a `require_review` proposal that stages this call would hit the identical problem at accept time. `resolve_contradiction()`'s own analogous check (KI-044) also raises outright for the same reason, and this ADR mirrors that precedent rather than inventing a new one.

**Why "at least one eligible," not "both must be eligible":** a contradiction only needs one eligible winner for `resolve_contradiction()` to succeed — the other member can already be terminal (e.g. the common case of flagging a still-active assertion against one a concurrent write already retracted). Requiring both to be non-terminal would reject legitimate contradictions unnecessarily; requiring *none* (today's pre-fix behavior) is the actual gap. "At least one" is the precise boundary `resolve_contradiction()`'s own requirement implies.

**Why the check doesn't apply to the "extend" branch:** ADR-0031 already made a deliberate, documented decision that extending an open contradiction with a terminal-status member is legitimate — it's how a caller names a terminal assertion for audit/context without resurrecting it. Extending never creates a *new* contradiction with zero eligible winners in isolation either: the contradiction already has whatever eligibility state its prior members left it in, and this call doesn't remove eligible members, only adds (possibly terminal) ones. Blocking extension here would re-litigate a decision this ADR isn't reopening.

**Why this doesn't affect the `get_open_contradiction` lookup itself:** the check runs on `a`/`b`'s *current* status, read fresh inside the transaction (KI-045) — no change to when or how that lookup happens, no new TOCTOU surface introduced.

## Consequences

**Positive:**
- Closes KI-050: a `propose`-capability caller (including an AI principal over MCP) can no longer open a contradiction with zero eligible winners without knowing to immediately work around it.
- No new capability-checking machinery, no new backend round-trip — `a`/`b` are already fetched by this point in the method for the subject/predicate check.

**Negative / follow-ups:**
- **Breaking (observable, not signature-level):** a caller that previously relied on `flag_contradiction()` succeeding with two already-terminal assertion IDs (to pre-stage a contradiction before a corroborating write, for instance) now gets `ValidationError` instead. The corrected known-issues.md entry's own analysis found no legitimate use case this forecloses — such a caller can create the contradiction with at least one non-terminal member, or extend an existing one — but it is a behavior change for any caller doing this today.
- Mirrors `resolve_contradiction()`'s check closely enough that the two could in principle share a helper; not done here since the two live in different methods with different surrounding logic (one gates contradiction *creation*, the other gates *winner selection*) and the duplication is two lines, not judged worth the indirection.

## Alternatives Considered

- **Route to review instead of rejecting outright:** rejected — see Rationale. There's no reviewable state that resolves two terminal assertion IDs into eligible ones; this is a structural validation, not a capability gate.
- **Reject if *either* member is terminal (require both non-terminal):** rejected — over-broad. The common, legitimate case of flagging a live assertion against one a concurrent write already terminalized would be rejected unnecessarily; "at least one eligible" is the precise requirement `resolve_contradiction()` actually has.
- **Extend the check to the "extend" branch too:** rejected — would reopen ADR-0031's own deliberate decision to permit naming a terminal assertion when extending, not something this ADR's narrower scope (KI-050) asks for or should decide implicitly.
- **Leave it as an accepted, documented gap** (a caller must know to avoid an all-terminal creation): rejected — the gap is cheap to close, has a precise, unambiguous boundary borrowed directly from an already-established precedent (ADR-0031), and silently leaving a `propose`-capability, MCP-reachable action able to open an unresolvable-without-a-workaround contradiction serves no one.
