# ADR-0025: `PolicyStrategy.evaluate()`'s `kb` Parameter and `SourceQuorum`

**Status:** Accepted

**Date:** 2026-07-27

**Deciders:** Ontolith Core Team

**Related:** ADR-0018 (`PolicyStrategy` injection — deferred `kb`), SPEC §9.2 (Policy engine
contract), §14 (Plugin protocols)

## Context

ADR-0018 made `PolicyStrategy` actually injectable but deliberately deferred adding SPEC §9.2's
`kb: ReadOnlyView` parameter to `evaluate()`, for two stated reasons: no concrete KB-inspecting
strategy (`SourceQuorum`, one of SPEC §9.2's SHOULD-have strategies) existed yet to validate the
shape against, and a *live* KB view doesn't satisfy §9.2's "testable and replayable" purity
requirement — there's no fixed state to reproduce a decision against later. KI-017 tracked this
gap. This ADR resolves it: `SourceQuorum` is now the concrete consumer, and the "replayable"
question has a concrete answer.

Two facts, verified directly against the code rather than assumed, shape the design:

**All three `evaluate()` call sites run strictly before any backend write.** In `Ontology.propose`/
`propose_ref`/`retract` (`ontology.py`), `now = self.clock.now()` is computed and used as both
`proposal.created_at` and the timestamp any resulting `Assertion` would carry; `evaluate()` runs
immediately after, entirely in memory; only if the decision is `AutoAccept` does
`self.backend.transaction()` persist anything. So a KB view pinned at `proposal.created_at`
naturally can't see the in-flight proposal's own operation (nothing is written yet), and
re-running `Ontology.as_of(proposal.created_at)` later reproduces the exact same read — this is a
concrete answer to "replayable against what state." `Ontology.as_of()` already exists and returns
`AsOfView`, which does no I/O at construction (only `.assertions()`/`.query()` touch the backend),
so passing one into every `evaluate()` call costs nothing when a strategy doesn't use it.

**SPEC's literal `kb: ReadOnlyView` type is the wrong type for this purpose, independent of any
import concern.** `ontolith.plugins.views.ReadOnlyView` wraps a *live* `Ontology`
(`ReadOnlyView.__init__(self, kb: Ontology, principal_id: str)`) — it's the same live-state
problem ADR-0018 already rejected, just one level removed (its own `.as_of(t)` delegates back to
`Ontology.as_of()`, proving even `ReadOnlyView`'s own author needed a pinned view for time-travel).
It also isn't a `Protocol` — nominal typing — and `AsOfView` (the type that's actually
constructible and correct here) neither inherits from nor is related to it. Importing either
concrete class into `govern/policy.py` for the type hint is also a genuine circular import:
`plugins/views.py` imports `Decision` from `govern.policy`, and `ontology.py` (home of `AsOfView`)
imports `PolicyStrategy`/`ThresholdPolicy` from `govern.policy` too — both concrete classes live in
modules that already depend on this one.

Grepping every `.evaluate(` call site and `def evaluate(` definition in the repository sized the
blast radius precisely before deciding the signature shape: two files define `evaluate()`
(`govern/policy.py`, and `conformance/test_policy_injection.py`'s two custom `PolicyStrategy` test
doubles); roughly 25 more call sites across `tests/unit/test_govern.py` and three conformance files
call `ThresholdPolicy().evaluate(...)` on a locally-typed `ThresholdPolicy` variable, never through
the abstract `PolicyStrategy` type — so mypy checks those against `ThresholdPolicy`'s own concrete
signature, not the Protocol's.

## Decision

**1. `KbView` — a minimal structural Protocol, declared locally in `govern/policy.py`:**
```python
class KbView(Protocol):
    def assertions(
        self, subject: str | None = None, predicate: str | None = None
    ) -> list[Assertion]: ...
```
Both `AsOfView` and `ReadOnlyView` already satisfy this shape with no inheritance or import
required — the same "abstract port, not a concrete adapter" pattern already used for
`StorageBackend`/`Clock`/`IdProvider`. `Assertion` is imported only under `TYPE_CHECKING` (with
`from __future__ import annotations`), matching the pattern `govern/conflict.py` already uses for
exactly this kind of type-only need.

**2. `PolicyStrategy.evaluate()` gains `kb: KbView`, required, inserted between `principal` and
`acting_as`** — matching SPEC's `(proposal, principal, kb)` ordering exactly; `acting_as` remains
the one documented ontolith-specific trailing addition from ADR-0018.

**3. `ThresholdPolicy.evaluate()`'s own concrete signature gives `kb` a default:
`kb: KbView | None = None`, accepted but never read.** `ThresholdPolicy`'s decisions depend only on
`principal`/`acting_as`; requiring every one of the ~25 pre-existing pure tests of this class to
construct and pass a KB view it would never use is pure churn with no signal. A concrete
implementation is free to accept a superset of what its Protocol requires — the Protocol still
requires `kb` for anyone who declares a variable typed as `PolicyStrategy` (which is what all three
real `Ontology` call sites do, always passing a real view).

**4. `Ontology.propose`/`propose_ref`/`retract` each construct `kb_view = self.as_of(now)`**
(reusing the already-computed `now`) and pass it positionally into `evaluate()`.

**5. New `SourceQuorum` strategy**, SPEC §9.2's KB-inspecting example: auto-accepts once
`threshold` distinct sources corroborate the same `(subject, predicate, value)`. Counts the
proposal's own source together with `kb`-visible assertions on the same triple that carry a
non-`None` source — a fact with no recorded source can't establish independent corroboration, so
it's excluded. Retractions and sourceless proposals always require review (a retraction isn't a
corroborable fact; a quorum can't be established without a proposing source). Deliberately does
**not** special-case AI-authored proposals: `ThresholdPolicy`'s "AI always requires review" rule
(ADR-0003) is that strategy's own design choice, not a cross-cutting invariant every
`PolicyStrategy` must reimplement — combining a source-quorum rule with an AI-review rule is what
SPEC §9.2's `Composite` strategy is for, not built here (still unbuilt — out of scope for KI-017).

## Rationale

**Why a local structural Protocol instead of importing `ReadOnlyView`:** two independent reasons,
either one sufficient alone. Correctness: `ReadOnlyView` is a live view, and passing one to
`evaluate()` would silently reintroduce the exact non-replayability problem ADR-0018 flagged —
using it wouldn't just be inconvenient, it would be *wrong*. Mechanics: doing so would also be a
circular import regardless. A minimal Protocol sidesteps both — it's the smallest common shape both
the correct type (`AsOfView`) and the SPEC-named type (`ReadOnlyView`) already satisfy.

**Why pin the snapshot at `proposal.created_at` rather than "evaluation time" or some other
instant:** `proposal.created_at` already exists, is already the timestamp used for the assertion
that would result from an `AutoAccept`, and is computed exactly once before any decision-affecting
work happens — there's no meaningfully different "evaluation time" in the current single-threaded,
synchronous call sequence to pin against instead. Using it also requires zero new fields on
`Proposal` or bookkeeping anywhere.

**Why `ThresholdPolicy` gets a default and `SourceQuorum` doesn't:** the Protocol's job is to
describe what `Ontology` guarantees it will pass and what a KB-inspecting strategy may rely on
receiving; a strategy's own concrete signature is free to be more permissive about what it accepts
when it simply doesn't need the extra input. Making `kb` required on the Protocol (matching SPEC,
and giving `SourceQuorum` a signature that fails loudly instead of silently if ever called without
one) was judged more valuable than giving the Protocol itself a default "for symmetry."

## Alternatives Considered

**Pass a live `ReadOnlyView`/`Ontology` reference instead of a pinned `AsOfView`:** rejected — this
is precisely the non-replayable shape ADR-0018 already rejected; nothing about building
`SourceQuorum` changed that constraint.

**Import `ReadOnlyView` or `AsOfView` directly into `govern/policy.py`, guarded by
`TYPE_CHECKING`:** technically avoids the *runtime* circular import (a `TYPE_CHECKING`-only import
never executes), but doesn't fix the deeper problem — `ReadOnlyView` remains the wrong (live, not
pinned) type to name, and `AsOfView` lives in `ontology.py`, which imports `PolicyStrategy` from
this same module, so naming it here would still couple a pure domain Protocol's type identity to a
concrete class one layer up. Rejected in favor of the structural Protocol, which needs neither.

**Give `PolicyStrategy.evaluate()`'s own `kb` parameter a default:** rejected — SPEC's signature has
none, and `SourceQuorum` needs it to be meaningfully required so a caller can't silently construct
one without a KB view and get nonsense results; the churn this ADR is avoiding belongs to
`ThresholdPolicy`'s own concrete signature, not the Protocol's contract.

**Have `SourceQuorum` special-case AI-authored proposals like `ThresholdPolicy` does:** rejected —
conflating "how many sources corroborate this" with "who authored it" inside one strategy
duplicates logic `Composite` (SPEC §9.2) already exists to combine; not needed to close KI-017.

## Consequences

**Positive:** `PolicyStrategy.evaluate()` now matches SPEC §9.2's parameter list exactly (modulo
the already-documented `acting_as` addition). `SourceQuorum` is fully usable via
`Ontology(policy=SourceQuorum(threshold=N))`. The replayability question ADR-0018 left open has a
concrete, tested answer for future KB-inspecting strategies (`ConfidenceThreshold`, `TrustLevel`,
`SourceRequired`, `RequireReviewByRole`, `Composite` — still unbuilt, but now unblocked by this same
`kb`/`KbView` machinery).

**Negative / follow-ups:** `PolicyStrategy.evaluate()`'s required-parameter list is a breaking
change for any third-party implementer (a new required positional parameter) — flagged
`**Breaking:**` in `CHANGELOG.md` per ADR-0019's public API stability policy. `Composite` and the
other three SHOULD-have strategies named above remain unbuilt; nothing about this ADR blocks them.

## References

- SPEC §9.2 (Policy engine contract), §14 (Plugin protocols — `PolicyStrategy`)
- ADR-0018 (`PolicyStrategy` injection, deferred `kb` — this ADR resolves that deferral)
- ADR-0003 (Agent identity — `ThresholdPolicy`'s AI-always-review rule, not reimplemented here)
- ADR-0019 (Public API stability policy — breaking-change classification for the signature change)
- `docs/known-issues.md` KI-017 (now resolved)
