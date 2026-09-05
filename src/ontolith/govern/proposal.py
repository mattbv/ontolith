"""Proposal model - staged operations awaiting policy decision.

Per SPEC §9: Proposals are the write path for assertions. They contain staged
operations that are evaluated by the policy engine before being applied.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Named alias (not inlined in Proposal.state below) so every interface that
# validates a caller-supplied state filter (REST/GraphQL's list routes,
# KI-077; MCP's own list tools) can derive its accepted-values set via
# ``typing.get_args(ProposalState)`` from this one source of truth, instead
# of a hand-duplicated tuple that could silently drift if this Literal ever
# gains or loses a state — same pattern ``plugins/registry.py`` already uses
# for ``PluginKind``.
ProposalState = Literal[
    "draft",
    "submitted",
    "auto_accepted",
    "require_review",
    "under_review",
    "accepted",
    "rejected",
    "changes_requested",
]


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
        reviewers: Principal IDs assigned to review this proposal
        payload: Staged operations as JSON
        metadata: Open JSON blob
    """

    # Identity
    id: str
    namespace: str
    author: str
    acting_as: str | None = None

    # State machine (SPEC §9.1)
    state: ProposalState = "draft"

    # Temporal
    created_at: datetime
    decided_at: datetime | None = None

    # Policy
    policy_reason: str | None = None
    # Set from the policy's own RequireReview.reviewers at creation time
    # (Ontology._finalize_non_accepted_decision) and refreshed on resubmit
    # (KI-027) or an explicit Ontology.assign_reviewers() call (SPEC §9.4's
    # `assign` action). Not cleared on accept/reject/request_changes — it
    # remains a historical record of who was asked, same as policy_reason
    # (KI-078: previously computed by every PolicyStrategy but never
    # persisted or surfaced anywhere).
    reviewers: list[str] = Field(default_factory=list)

    # Operations
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)


class ProposalEvent(BaseModel):
    """A structured action recorded against a proposal (SPEC §9.4 plus
    `resubmit`, KI-027).

    Covers four of SPEC §9.4's named review actions implemented as Ontology
    methods (accept, reject, request_changes, assign) and `resubmit` — an
    author action, not a reviewer one, but recorded here anyway because
    `resubmit` re-evaluates policy against an already-persisted proposal
    (ADR-0025's `kb_view` pin is the resubmission instant, not
    `proposal.created_at`) and without an event, a `require_review`
    outcome would leave no trace of when that evaluation happened.
    `comment` is not implemented as a method yet, so no event type exists
    for it — this is a scoped subset of SPEC §9.4's full action vocabulary,
    not the complete review workflow.

    Attributes:
        id: Unique event ID (ULID)
        proposal_id: Proposal this event was recorded against
        actor: Principal ID who performed the action
        type: Which action this event records
        detail: Optional free-text detail (e.g. a rejection reason, or the
            comma-joined reviewer list for `assign`)
        at: When the action occurred
    """

    id: str
    proposal_id: str
    actor: str
    type: Literal["accept", "reject", "request_changes", "resubmit", "assign"]
    detail: str | None = None
    at: datetime

    model_config = ConfigDict(frozen=True)


__all__ = ["Proposal", "ProposalEvent", "ProposalState"]
