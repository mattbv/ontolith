"""Policy engine - pure functions for proposal evaluation.

Per SPEC §9.2: Policy is a pure function that takes a proposal and principal
and returns a decision (auto-accept, require review, or reject).
"""

from typing import Protocol

from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal


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


class PolicyStrategy(Protocol):
    """Protocol for policy strategies.

    Policy strategies are PURE functions - no I/O, deterministic, testable.
    """

    def evaluate(self, proposal: Proposal, principal: Principal) -> Decision:
        """Evaluate a proposal and return a decision.

        Args:
            proposal: Proposal to evaluate
            principal: Principal who created the proposal

        Returns:
            Decision (AutoAccept, RequireReview, or Reject)
        """
        ...


class ThresholdPolicy:
    """Threshold policy evaluating capability and trust level.

    Rules (evaluated in order):
    - Human/service with write/review/admin capability: auto-accept
    - AI principals: always require review, regardless of trust level (ADR-0003)
    - Read-only principals: reject immediately (no propose rights)
    - Non-AI principals with propose capability + trust_level >= 5: auto-accept
    - Everyone else: require review
    """

    def evaluate(self, proposal: Proposal, principal: Principal) -> Decision:
        """Evaluate proposal based on principal capabilities and trust level."""
        # Humans with write capability can auto-accept
        if principal.kind == "human" and principal.default_capability in (
            "write",
            "review",
            "admin",
        ):
            return AutoAccept(f"Trusted human principal ({principal.id})")

        # Service principals with write can auto-accept
        if principal.kind == "service" and principal.default_capability in (
            "write",
            "admin",
        ):
            return AutoAccept(f"Trusted service principal ({principal.id})")

        # AI always requires review — trust level never overrides this (ADR-0003)
        if principal.kind == "ai":
            reviewers = [principal.owner] if principal.owner else []
            return RequireReview(
                reviewers=reviewers,
                reason=f"AI proposals require review (owner: {principal.owner})",
            )

        # Read-only principals cannot propose — reject immediately
        if principal.default_capability == "read":
            return Reject(f"Principal {principal.id} has read-only access and cannot propose")

        # Trust-elevated propose: sufficient trust lifts a non-AI propose-capability principal
        if principal.default_capability == "propose" and principal.trust_level >= 5:
            return AutoAccept(
                f"Trust-elevated principal ({principal.id}, trust={principal.trust_level})"
            )

        # Default: require review
        return RequireReview(
            reviewers=[],
            reason=f"Principal {principal.id} requires review approval",
        )


__all__ = ["Decision", "AutoAccept", "RequireReview", "Reject", "PolicyStrategy", "ThresholdPolicy"]
