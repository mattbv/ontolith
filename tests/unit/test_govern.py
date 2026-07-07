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


class TestThresholdPolicyDelegation:
    """acting_as (SPEC §8.4): effective capability is min(author, acting_as),
    never a wholesale substitution. AI-kind authors always require review
    regardless of delegation (ADR-0003)."""

    def _principal(self, **overrides: object) -> Principal:
        base: dict[str, object] = {
            "id": "p",
            "kind": "human",
            "auth_method": "oidc",
            "default_capability": "propose",
            "trust_level": 0,
            "created_at": datetime(2025, 1, 1, tzinfo=UTC),
        }
        base.update(overrides)
        return Principal(**base)  # type: ignore[arg-type]

    def _proposal(self, author: str = "author") -> Proposal:
        return Proposal(
            id="prop-001",
            namespace="test",
            author=author,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

    def test_ai_author_always_requires_review_regardless_of_delegate_capability(self) -> None:
        """AI author delegating to a write-capable owner still requires review —
        this is the core delegation-bypass fix: AI's own kind must dominate."""
        policy = ThresholdPolicy()
        ai = self._principal(id="bot", kind="ai", owner="owner@example.com")
        owner = self._principal(id="owner@example.com", default_capability="write")

        decision = policy.evaluate(self._proposal("bot"), ai, acting_as=owner)

        assert isinstance(decision, RequireReview)

    def test_propose_delegating_to_write_capped_at_propose(self) -> None:
        """min(propose, write) = propose — the delegate's higher capability does
        not elevate the author (without also meeting the trust>=5 threshold)."""
        policy = ThresholdPolicy()
        author = self._principal(id="author", default_capability="propose", trust_level=0)
        delegate = self._principal(id="delegate", default_capability="write")

        decision = policy.evaluate(self._proposal("author"), author, acting_as=delegate)

        assert isinstance(decision, RequireReview)

    def test_write_delegating_to_propose_capped_at_propose(self) -> None:
        """min(write, propose) = propose — delegating "up" doesn't help either;
        the lower of the two capabilities always governs."""
        policy = ThresholdPolicy()
        author = self._principal(id="author", default_capability="write")
        delegate = self._principal(id="delegate", default_capability="propose", trust_level=0)

        decision = policy.evaluate(self._proposal("author"), author, acting_as=delegate)

        assert isinstance(decision, RequireReview)

    def test_write_delegating_to_write_auto_accepts(self) -> None:
        """min(write, write) = write — both sides sufficiently capable auto-accepts."""
        policy = ThresholdPolicy()
        author = self._principal(id="author", default_capability="write")
        delegate = self._principal(id="delegate", default_capability="write")

        decision = policy.evaluate(self._proposal("author"), author, acting_as=delegate)

        assert isinstance(decision, AutoAccept)

    def test_effective_trust_level_is_the_lower_of_the_two(self) -> None:
        """min(trust_level) governs the propose+trust>=5 auto-accept branch —
        a high-trust delegate cannot lift a low-trust author over the threshold."""
        policy = ThresholdPolicy()
        author = self._principal(id="author", default_capability="propose", trust_level=0)
        delegate = self._principal(id="delegate", default_capability="propose", trust_level=10)

        decision = policy.evaluate(self._proposal("author"), author, acting_as=delegate)

        assert isinstance(decision, RequireReview)

    def test_no_delegation_matches_undelegated_evaluate(self) -> None:
        """acting_as=None behaves identically to the 2-arg call (backward compatible)."""
        policy = ThresholdPolicy()
        author = self._principal(id="author", default_capability="write")

        with_none = policy.evaluate(self._proposal("author"), author, acting_as=None)
        without_arg = policy.evaluate(self._proposal("author"), author)

        assert isinstance(with_none, AutoAccept)
        assert isinstance(without_arg, AutoAccept)
