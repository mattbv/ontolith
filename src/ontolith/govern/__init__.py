"""Governance layer - proposals, policy, review, conflict resolution.

This module handles the write path and governance:
- Proposals: staged operations awaiting approval
- Policy: pure functions for auto-accept/review decisions
- Review: human approval workflow (M2)
- Conflict: temporal supersession and contradictions (M2)
"""

from ontolith.govern.conflict import Activate, ConflictResult, Contradict, Supersede, route
from ontolith.govern.contradiction import Contradiction
from ontolith.govern.policy import (
    AutoAccept,
    Decision,
    PolicyStrategy,
    Reject,
    RequireReview,
    ThresholdPolicy,
)
from ontolith.govern.proposal import Proposal

__all__ = [
    "Proposal",
    "Contradiction",
    "Decision",
    "AutoAccept",
    "RequireReview",
    "Reject",
    "PolicyStrategy",
    "ThresholdPolicy",
    "Activate",
    "Supersede",
    "Contradict",
    "ConflictResult",
    "route",
]
