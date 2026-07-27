"""Policy engine - pure functions for proposal evaluation.

Per SPEC §9.2: Policy is a pure function that takes a proposal and principal
and returns a decision (auto-accept, require review, or reject).
"""

from __future__ import annotations

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
    """

    def assertions(
        self, subject: str | None = None, predicate: str | None = None
    ) -> list[Assertion]:
        """Assertions visible to this view, optionally filtered."""
        ...


class PolicyStrategy(Protocol):
    """Protocol for policy strategies.

    Policy strategies are PURE functions - no I/O, deterministic, testable.
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

    Retractions and sourceless proposals always require review: a
    retraction isn't a corroborable fact, and a quorum can't be established
    without knowing the proposing source.

    Does not special-case AI-authored proposals: ``ThresholdPolicy``'s
    "AI always requires review" rule (ADR-0003) is that strategy's own
    design choice, not a cross-cutting invariant every ``PolicyStrategy``
    must reimplement. Combining a source-quorum rule with an AI-review rule
    is what SPEC §9.2's ``Composite`` strategy is for — not built here.
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
        self.reviewers = reviewers if reviewers is not None else []

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate proposal based on distinct-source corroboration count."""
        op = proposal.payload["operations"][0]

        if op["kind"] == "retract":
            return RequireReview(self.reviewers, "SourceQuorum does not auto-accept retractions")

        source = op.get("source")
        if not source:
            return RequireReview(
                self.reviewers,
                f"Proposal has no source; quorum of {self.threshold} cannot be established",
            )

        value = op["value"] if op["kind"] == "assert_literal" else op["target"]
        existing = kb.assertions(subject=op["subject"], predicate=op["predicate"])
        sources = {a.source for a in existing if a.value == value and a.source}
        sources.add(source)

        if len(sources) >= self.threshold:
            return AutoAccept(f"Source quorum reached ({len(sources)}/{self.threshold})")
        return RequireReview(
            self.reviewers,
            f"Source quorum not met ({len(sources)}/{self.threshold} distinct sources)",
        )


__all__ = [
    "Decision",
    "AutoAccept",
    "RequireReview",
    "Reject",
    "KbView",
    "PolicyStrategy",
    "ThresholdPolicy",
    "SourceQuorum",
]
