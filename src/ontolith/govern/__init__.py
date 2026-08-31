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
    Composite,
    Decision,
    KbView,
    PolicyStrategy,
    Reject,
    RequireReview,
    SourceQuorum,
    ThresholdPolicy,
)
from ontolith.govern.proposal import Proposal, ProposalEvent

__all__ = [
    "Proposal",
    "ProposalEvent",
    "Contradiction",
    "Decision",
    "AutoAccept",
    "RequireReview",
    "Reject",
    "KbView",
    "PolicyStrategy",
    "ThresholdPolicy",
    "SourceQuorum",
    "Composite",
    "Activate",
    "Supersede",
    "Contradict",
    "ConflictResult",
    "route",
]
