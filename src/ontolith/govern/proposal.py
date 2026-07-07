"""Proposal model - staged operations awaiting policy decision.

Per SPEC §9: Proposals are the write path for assertions. They contain staged
operations that are evaluated by the policy engine before being applied.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Proposal(BaseModel):
    """Staged operations awaiting policy decision.

    Attributes:
        id: Unique proposal ID (ULID)
        namespace: Namespace this proposal applies to
        author: Principal ID who created the proposal
        acting_as: Optional delegation
        state: Current state in the workflow
        created_at: When the proposal was created
        decided_at: When the proposal was accepted/rejected
        policy_reason: Why the policy made its decision
        payload: Staged operations as JSON
        metadata: Open JSON blob
    """

    # Identity
    id: str
    namespace: str
    author: str
    acting_as: str | None = None

    # State machine (SPEC §9.1)
    state: Literal[
        "draft",
        "submitted",
        "auto_accepted",
        "require_review",
        "under_review",
        "accepted",
        "rejected",
        "changes_requested",
    ] = "draft"

    # Temporal
    created_at: datetime
    decided_at: datetime | None = None

    # Policy
    policy_reason: str | None = None

    # Operations
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)


class ProposalEvent(BaseModel):
    """A structured review action recorded against a proposal (SPEC §9.4).

    Currently covers the two review actions that exist as Ontology methods:
    accept and reject. assign/comment/request_changes are not implemented as
    methods yet, so no event type exists for them — this is a scoped subset
    of SPEC §9.4's full action vocabulary, not the complete review workflow.

    Attributes:
        id: Unique event ID (ULID)
        proposal_id: Proposal this event was recorded against
        actor: Principal ID who performed the review action
        type: Which review action this event records
        detail: Optional free-text detail (e.g. a rejection reason)
        at: When the action occurred
    """

    id: str
    proposal_id: str
    actor: str
    type: Literal["accept", "reject"]
    detail: str | None = None
    at: datetime

    model_config = ConfigDict(frozen=True)


__all__ = ["Proposal", "ProposalEvent"]
