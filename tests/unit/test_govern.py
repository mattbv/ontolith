"""Unit tests for governance layer."""

from datetime import UTC, datetime

import pytest

from ontolith.govern import AutoAccept, Proposal, RequireReview, ThresholdPolicy
from ontolith.identity import Principal


class TestProposal:
    """Tests for Proposal model."""

    def test_create_proposal(self) -> None:
        """Proposal can be created."""
        proposal = Proposal(
            id="prop-001",
            namespace="test-ns",
            author="alice@example.com",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            payload={"operations": [{"type": "create_entity"}]},
        )

        assert proposal.id == "prop-001"
        assert proposal.author == "alice@example.com"
        assert proposal.state == "draft"
        assert proposal.decided_at is None

    def test_proposal_immutable(self) -> None:
        """Proposals are immutable."""
        from pydantic import ValidationError

        proposal = Proposal(
            id="prop-001",
            namespace="test",
            author="alice@example.com",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        with pytest.raises(ValidationError):
            proposal.state = "accepted"  # type: ignore


class TestThresholdPolicy:
    """Tests for ThresholdPolicy."""

    def test_human_with_write_auto_accepts(self) -> None:
        """Humans with write capability auto-accept."""
        policy = ThresholdPolicy()

        principal = Principal(
            id="alice@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        proposal = Proposal(
            id="prop-001",
            namespace="test",
            author=principal.id,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        decision = policy.evaluate(proposal, principal)

        assert isinstance(decision, AutoAccept)
        assert "Trusted human" in decision.reason

    def test_ai_requires_review(self) -> None:
        """AI principals always require review."""
        policy = ThresholdPolicy()

        principal = Principal(
            id="bot-001",
            kind="ai",
            owner="alice@example.com",
            auth_method="workload",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        proposal = Proposal(
            id="prop-001",
            namespace="test",
            author=principal.id,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        decision = policy.evaluate(proposal, principal)

        assert isinstance(decision, RequireReview)
        assert principal.owner in decision.reviewers
        assert "AI proposals require review" in decision.reason

    def test_human_with_propose_requires_review(self) -> None:
        """Humans with only propose capability need review."""
        policy = ThresholdPolicy()

        principal = Principal(
            id="bob@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="propose",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        proposal = Proposal(
            id="prop-001",
            namespace="test",
            author=principal.id,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        decision = policy.evaluate(proposal, principal)

        assert isinstance(decision, RequireReview)

    def test_service_with_write_auto_accepts(self) -> None:
        """Service principals with write auto-accept."""
        policy = ThresholdPolicy()

        principal = Principal(
            id="import-service",
            kind="service",
            auth_method="apikey",
            default_capability="write",
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        proposal = Proposal(
            id="prop-001",
            namespace="test",
            author=principal.id,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        decision = policy.evaluate(proposal, principal)

        assert isinstance(decision, AutoAccept)
        assert "Trusted service" in decision.reason
