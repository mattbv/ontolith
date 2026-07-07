"""Policy engine - pure functions for proposal evaluation.

Per SPEC §9.2: Policy is a pure function that takes a proposal and principal
and returns a decision (auto-accept, require review, or reject).
"""

from typing import Protocol

from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal, min_capability


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

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate a proposal and return a decision.

        Args:
            proposal: Proposal to evaluate
            principal: Principal who authored the proposal
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
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate proposal based on principal capabilities and trust level."""
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


__all__ = ["Decision", "AutoAccept", "RequireReview", "Reject", "PolicyStrategy", "ThresholdPolicy"]
