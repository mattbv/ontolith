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
    """Simple threshold policy for M1.

    Rules:
    - Human principals with write capability: auto-accept
    - AI principals: require review (future: check confidence threshold)
    - Service principals with write capability: auto-accept
    - Everyone else: require review
    """

    def evaluate(self, proposal: Proposal, principal: Principal) -> Decision:
        """Evaluate proposal based on principal capabilities."""
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

        # AI always requires review in M1
        if principal.kind == "ai":
            # In M1, route to owner for review
            reviewers = [principal.owner] if principal.owner else []
            return RequireReview(
                reviewers=reviewers,
                reason=f"AI proposals require review (owner: {principal.owner})",
            )

        # Default: require review
        return RequireReview(
            reviewers=[],
            reason=f"Principal {principal.id} requires review approval",
        )


__all__ = ["Decision", "AutoAccept", "RequireReview", "Reject", "PolicyStrategy", "ThresholdPolicy"]
