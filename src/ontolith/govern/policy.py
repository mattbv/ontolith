"""Policy engine - pure functions for proposal evaluation.

Per SPEC §9.2: Policy is a pure function that takes a proposal and principal
and returns a decision (auto-accept, require review, or reject). "Pure" means
no writes and deterministic given its inputs — a strategy MAY read via ``kb``
(a pinned snapshot, not live KB state) without violating this.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal, min_capability

if TYPE_CHECKING:
    from ontolith.core import Assertion


class Decision:
    """Base class for policy decisions."""

    pass


class AutoAccept(Decision):
    """Automatically accept this proposal."""

    reason: str

    def __init__(self, reason: str = "Policy approved") -> None:
        self.reason = reason


class RequireReview(Decision):
    """Require human review before accepting."""

    reviewers: list[str]
    reason: str

    def __init__(self, reviewers: list[str], reason: str) -> None:
        self.reviewers = reviewers
        self.reason = reason


class Reject(Decision):
    """Reject this proposal."""

    reason: str

    def __init__(self, reason: str) -> None:
        self.reason = reason


class KbView(Protocol):
    """Structural read view a PolicyStrategy may query for KB-inspecting decisions.

    Deliberately narrower than SPEC §9.2's literal ``kb: ReadOnlyView`` — that
    class (``ontolith.plugins.views.ReadOnlyView``) wraps a *live*
    ``Ontology`` and isn't even the right type here (§9.2 requires
    ``evaluate`` to be "testable and replayable", which a live, unpinned view
    can't satisfy). Real callers pass a bitemporally-pinned ``AsOfView``
    (``ontology.py``) instead. Importing either concrete class into this
    module would also be circular: both live in modules that import
    ``Decision``/``PolicyStrategy`` from here. This minimal Protocol is the
    common shape both classes already satisfy structurally, with no
    inheritance or import required (KI-017, ADR-0025).

    The two concrete views a strategy might actually receive have different
    runtime semantics for the same call: ``AsOfView`` (what ``Ontology``
    passes) is temporally pinned and excludes flagged assertions by default;
    ``ReadOnlyView`` is live and defaults to ``status="active"``. Real
    callers only ever pass ``AsOfView`` today.
    """

    def assertions(
        self, subject: str | None = None, predicate: str | None = None
    ) -> list[Assertion]:
        """Assertions visible to this view, optionally filtered."""
        ...


class PolicyStrategy(Protocol):
    """Protocol for policy strategies.

    Policy strategies are PURE functions: no writes, deterministic given
    their inputs, testable. Reading via ``kb`` is permitted — it's a pinned
    snapshot (ADR-0025), not live, mutable KB state.
    """

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate a proposal and return a decision.

        Args:
            proposal: Proposal to evaluate
            principal: Principal who authored the proposal
            kb: Read view of the KB, pinned to the proposal's creation time
                (SPEC §9.2's ``kb`` parameter — ADR-0025)
            acting_as: Principal being delegated to, if any (SPEC §8.4)

        Returns:
            Decision (AutoAccept, RequireReview, or Reject)
        """
        ...


class ThresholdPolicy:
    """Threshold policy evaluating capability and trust level.

    Rules (evaluated in order):
    - AI principals: always require review, regardless of trust level or
      delegation (ADR-0003) — the author's own kind is never laundered away
      by delegating to a higher-capability principal.
    - Human/service with write/review/admin capability: auto-accept
    - Read-only principals: reject immediately (no propose rights)
    - Non-AI principals with propose capability + trust_level >= 5: auto-accept
    - Everyone else: require review

    When ``acting_as`` is set (delegation), the effective capability is
    min(capability(principal), capability(acting_as)) and the effective trust
    level is the more conservative (lower) of the two (SPEC §8.4) — the
    delegating principal's capability is never substituted wholesale.
    """

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView | None = None,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate proposal based on principal capabilities and trust level.

        ``kb`` is accepted for ``PolicyStrategy`` conformance but never read —
        this strategy's decisions depend only on ``principal``/``acting_as``,
        so it defaults to ``None`` rather than requiring every caller
        (including the many pre-existing pure tests of this class) to
        construct a KB view it will never use (ADR-0025).
        """
        # AI always requires review — trust level and delegation never override
        # this (ADR-0003). Checked first, before any effective-capability math,
        # so an AI author's own kind can never be bypassed via acting_as.
        if principal.kind == "ai":
            reviewers = [principal.owner] if principal.owner else []
            return RequireReview(
                reviewers=reviewers,
                reason=f"AI proposals require review (owner: {principal.owner})",
            )

        capability: str = principal.default_capability
        trust_level = principal.trust_level
        if acting_as is not None:
            capability = min_capability(capability, acting_as.default_capability)
            trust_level = min(trust_level, acting_as.trust_level)

        # Humans with write capability can auto-accept
        if principal.kind == "human" and capability in ("write", "review", "admin"):
            return AutoAccept(f"Trusted human principal ({principal.id})")

        # Service principals with write can auto-accept
        if principal.kind == "service" and capability in ("write", "admin"):
            return AutoAccept(f"Trusted service principal ({principal.id})")

        # Read-only principals cannot propose — reject immediately
        if capability == "read":
            return Reject(f"Principal {principal.id} has read-only access and cannot propose")

        # Trust-elevated propose: sufficient trust lifts a non-AI propose-capability principal
        if capability == "propose" and trust_level >= 5:
            return AutoAccept(f"Trust-elevated principal ({principal.id}, trust={trust_level})")

        # Default: require review
        return RequireReview(
            reviewers=[],
            reason=f"Principal {principal.id} requires review approval",
        )


class SourceQuorum:
    """Auto-accepts once ``threshold`` distinct sources corroborate a fact (SPEC §9.2).

    Counts the proposal's own source together with existing assertions
    visible in ``kb`` (i.e. active as of the proposal's creation time) on the
    same ``(subject, predicate, value)`` that carry a non-``None`` source — an
    assertion with no recorded source can't establish independent
    corroboration, so it never counts toward the quorum.

    Rejects principals without at least ``propose`` capability, mirroring
    ``ThresholdPolicy``'s read-only rejection — with no other pre-write
    capability check in `Ontology.propose`/`propose_ref`/`retract`, KI-015's
    resolution requires every ``PolicyStrategy`` to enforce this floor itself
    (`docs/known-issues.md`).

    Retractions and sourceless proposals always require review: a
    retraction isn't a corroborable fact, and a quorum can't be established
    without knowing the proposing source. Only the proposal's first staged
    operation is inspected — every current caller (`propose`/`propose_ref`/
    `retract`) stages exactly one.

    Does not special-case AI-authored proposals: ``ThresholdPolicy``'s
    "AI always requires review" rule (ADR-0003) is that strategy's own
    design choice, not a cross-cutting invariant every ``PolicyStrategy``
    must reimplement — an AI-authored proposal CAN auto-accept here once
    quorum is reached (`conformance/test_source_quorum_policy.py` pins this
    as deliberate, unchanged behavior). A deployment that wants both rules
    combines them explicitly via ``Composite`` (KI-061, ADR-0040) — e.g.
    ``Composite(all=[SourceQuorum(2), some_ai_review_strategy])`` — rather
    than this class silently gaining an AI check of its own.

    Corroboration is checked against ``kb``'s pinned-at-``created_at`` state
    only — it does not compare the proposal's own ``valid_from``/``valid_to``
    against the matching existing assertions' validity windows. For a
    ``time_varying`` predicate with a backfilled or future-dated proposal,
    this can compare facts from different real-world periods; SourceQuorum
    is best suited to ``static`` properties.
    """

    def __init__(self, threshold: int, reviewers: list[str] | None = None) -> None:
        """Configure the strategy.

        Args:
            threshold: Minimum number of distinct sources required to
                auto-accept. Must be >= 1.
            reviewers: Reviewers assigned when the quorum isn't met.
                Defaults to no reviewers assigned.

        Raises:
            ValueError: threshold is less than 1
        """
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self.threshold = threshold
        self.reviewers = list(reviewers) if reviewers is not None else []

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate proposal based on capability and distinct-source corroboration count."""
        capability: str = principal.default_capability
        if acting_as is not None:
            capability = min_capability(capability, acting_as.default_capability)
        if capability == "read":
            return Reject(f"Principal {principal.id} has read-only access and cannot propose")

        operations = proposal.payload.get("operations") or []
        if not operations:
            return RequireReview(self.reviewers, "Proposal has no staged operations")
        op = operations[0]
        kind = op.get("kind")

        if kind == "retract":
            return RequireReview(self.reviewers, "SourceQuorum does not auto-accept retractions")
        if kind not in ("assert_literal", "assert_ref"):
            return RequireReview(
                self.reviewers, f"SourceQuorum does not recognize op kind {kind!r}"
            )

        source = op.get("source")
        if not source:
            return RequireReview(
                self.reviewers,
                f"Proposal has no source; quorum of {self.threshold} cannot be established",
            )

        value_kind = "literal" if kind == "assert_literal" else "ref"
        value = op["value"] if kind == "assert_literal" else op["target"]
        existing = kb.assertions(subject=op["subject"], predicate=op["predicate"])
        sources = {
            a.source
            for a in existing
            if a.value == value and a.value_kind == value_kind and a.source
        }
        sources.add(source)

        if len(sources) >= self.threshold:
            return AutoAccept(f"Source quorum reached ({len(sources)}/{self.threshold})")
        return RequireReview(
            self.reviewers,
            f"Source quorum not met ({len(sources)}/{self.threshold} distinct sources)",
        )


def _merge_reject(decisions: list[Reject]) -> Reject:
    """Combine multiple Reject decisions into one, concatenating reasons in
    input order (each retains its originating strategy's own wording)."""
    return Reject("; ".join(d.reason for d in decisions))


def _merge_require_review(decisions: list[RequireReview]) -> RequireReview:
    """Combine multiple RequireReview decisions into one: reviewers are a
    dedup'd union preserving first-seen order (the same reviewer named by
    two strategies is only assigned once); reasons are concatenated in
    input order."""
    reviewers: list[str] = []
    seen: set[str] = set()
    for d in decisions:
        for reviewer in d.reviewers:
            if reviewer not in seen:
                seen.add(reviewer)
                reviewers.append(reviewer)
    return RequireReview(reviewers, "; ".join(d.reason for d in decisions))


def _merge_auto_accept(decisions: list[AutoAccept]) -> AutoAccept:
    """Combine multiple AutoAccept decisions into one, concatenating reasons
    in input order."""
    return AutoAccept("; ".join(d.reason for d in decisions))


def _most_restrictive(decisions: list[Decision]) -> Decision:
    """The most restrictive decision among `decisions` (Reject > RequireReview
    > AutoAccept), merging every decision at that severity level.

    Used for `Composite(all=...)`: every strategy must independently agree
    to auto-accept — one Reject or RequireReview anywhere in the group
    overrides every AutoAccept the others returned, since "all" strategies
    must approve for the group as a whole to approve.
    """
    rejects = [d for d in decisions if isinstance(d, Reject)]
    if rejects:
        return _merge_reject(rejects)
    reviews = [d for d in decisions if isinstance(d, RequireReview)]
    if reviews:
        return _merge_require_review(reviews)
    accepts = [d for d in decisions if isinstance(d, AutoAccept)]
    return _merge_auto_accept(accepts)


def _least_restrictive(decisions: list[Decision]) -> Decision:
    """The least restrictive decision among `decisions` (AutoAccept >
    RequireReview > Reject), merging every decision at that severity level.

    Used for `Composite(any=...)`: only one strategy needs to approve — a
    single AutoAccept anywhere in the group is enough for the group as a
    whole to approve, regardless of what the others returned.
    """
    accepts = [d for d in decisions if isinstance(d, AutoAccept)]
    if accepts:
        return _merge_auto_accept(accepts)
    reviews = [d for d in decisions if isinstance(d, RequireReview)]
    if reviews:
        return _merge_require_review(reviews)
    rejects = [d for d in decisions if isinstance(d, Reject)]
    return _merge_reject(rejects)


class Composite:
    """Combines PolicyStrategy instances into one decision (SPEC §9.2's
    ``Composite(all=…, any=…)``).

    Every strategy in ``all`` must independently return ``AutoAccept`` for
    the ``all`` group to approve — a single ``Reject``/``RequireReview``
    anywhere in the group overrides every ``AutoAccept`` the others
    returned. At least one strategy in ``any`` must return ``AutoAccept``
    for the ``any`` group to approve. Both groups must approve for
    ``Composite`` itself to auto-accept (an unset group is excluded from
    the combination entirely, rather than contributing a placeholder
    decision — a caller passing only ``all=`` or only ``any=`` gets exactly
    that group's own semantics, unconstrained by the other).

    This is SPEC §9.2's sanctioned way to layer an unconditional rule (e.g.
    ThresholdPolicy's "AI principals always require review", ADR-0003) on
    top of a KB-inspecting strategy like ``SourceQuorum``, which
    deliberately does not special-case AI authorship on its own (KI-061,
    ADR-0025 §5). A deployment wanting both writes a small strategy for the
    unconditional half and composes it — Ontolith does not ship a
    standalone "AI always requires review" strategy, since ``ThresholdPolicy``
    bundles that rule with its own capability/trust-level checks (using the
    whole of ``ThresholdPolicy`` here would re-impose *its* capability gate
    too, defeating the point of choosing ``SourceQuorum`` in the first
    place)::

        class RequireReviewForAI:
            def evaluate(
                self,
                proposal: Proposal,
                principal: Principal,
                kb: KbView | None = None,
                acting_as: Principal | None = None,
            ) -> Decision:
                if principal.kind == "ai":
                    return RequireReview([principal.owner] if principal.owner else [],
                                          "AI proposals require review")
                return AutoAccept("non-AI: deferring to the rest of the Composite")

        policy = Composite(all=[RequireReviewForAI(), SourceQuorum(2)])

    When multiple strategies in the same group land at the same decision
    severity (e.g. two ``RequireReview``s), their reviewers are merged as a
    dedup'd union and their reasons are concatenated — every contributing
    strategy's rationale is preserved, not just the first one evaluated.

    Reads via ``kb`` are still permitted (this composes, not replaces, the
    contained strategies) — purity/determinism holds as long as every
    contained strategy holds it.

    ``Composite`` does not itself enforce KI-015's read-capability floor
    (``docs/known-issues.md``: "any future ``PolicyStrategy`` needs to make
    the same deliberate choice; it is not inherited for free") — it is
    exactly as permissive or restrictive as its contained strategies, by
    design, since it is a combinator rather than a leaf strategy. This is
    safe for ``all=``: any member's own ``Reject`` for a read-capability
    principal (e.g. ``SourceQuorum``'s) still wins, since ``all`` uses the
    most-restrictive decision. It is a real sharp edge for ``any=``: if one
    member in the group doesn't check capability at all and would
    otherwise ``AutoAccept``, that member's decision can win the group even
    though a stricter sibling (like ``SourceQuorum``) would have rejected
    the same principal — the same "OR" semantics that let one strategy's
    approval cover for another's stricter rule also let it cover for a
    missing capability check. Every strategy composed into an ``any=``
    group should enforce the floor itself if that matters for the
    deployment, the same way ``SourceQuorum`` already does.
    """

    def __init__(
        self,
        *,
        all: Sequence[PolicyStrategy] = (),
        any: Sequence[PolicyStrategy] = (),
    ) -> None:
        """Configure the strategy.

        Args:
            all: Strategies that must every one auto-accept.
            any: Strategies of which at least one must auto-accept.

        Raises:
            ValueError: neither `all` nor `any` has any strategies —
                a Composite with nothing to compose is a configuration
                mistake, not a meaningful policy.
        """
        if not all and not any:
            raise ValueError("Composite requires at least one strategy in `all` or `any`")
        # Stored as tuples, not lists, and under a private name: a public
        # mutable list would let `composite.all.clear()` silently empty a
        # group after construction, bypassing the `__init__` guard above
        # and making `evaluate()` fail open (found in review) instead of
        # raising as a freshly-emptied `Composite()` would.
        self._all = tuple(all)
        self._any = tuple(any)

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate every contained strategy and combine their decisions.

        Unlike ``ThresholdPolicy`` (which never reads ``kb`` and so
        defaults it to ``None`` for caller convenience), ``kb`` is required
        here — a contained strategy (e.g. ``SourceQuorum``) may genuinely
        need it, mirroring ``SourceQuorum``'s own required-``kb`` signature.
        """
        group_results: list[Decision] = []
        if self._all:
            group_results.append(
                _most_restrictive(
                    [s.evaluate(proposal, principal, kb, acting_as) for s in self._all]
                )
            )
        if self._any:
            group_results.append(
                _least_restrictive(
                    [s.evaluate(proposal, principal, kb, acting_as) for s in self._any]
                )
            )
        # __init__ guarantees at least one group is non-empty, and both are
        # immutable tuples set once at construction, so group_results is
        # never empty here.
        return _most_restrictive(group_results)


__all__ = [
    "Decision",
    "AutoAccept",
    "RequireReview",
    "Reject",
    "KbView",
    "PolicyStrategy",
    "ThresholdPolicy",
    "SourceQuorum",
    "Composite",
]
