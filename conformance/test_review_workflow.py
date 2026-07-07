"""Conformance vectors for SPEC §9 review workflow.

Tests accept_proposal() and reject_proposal() — the human-review path for
proposals that ThresholdPolicy routes to require_review (e.g. AI principals).
All tests use injected clocks and IDs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.core.errors import AuthError, CapabilityError, NotFoundError, ValidationError

T0 = datetime(2025, 1, 1, tzinfo=UTC)

HUMAN_AUTHOR = "alice@example.com"
REVIEWER = "bob@example.com"
AI_AUTHOR = "gpt-agent"
AI_OWNER = HUMAN_AUTHOR


def _kb(tmp_path: Path) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider(
        [
            "e-1",
            "a-1",
            "a-2",
            "a-3",
            "a-4",
            "prop-1",
            "prop-2",
            "prop-3",
            "prop-4",
            "contra-1",
        ]
    )
    kb = Ontology.connect(tmp_path / "test.db", clock=clock, id_provider=ids)
    kb.create_principal(HUMAN_AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(REVIEWER, kind="human", auth_method="oidc", default_capability="review")
    kb.create_principal(
        AI_AUTHOR, kind="ai", auth_method="apikey", owner=AI_OWNER, default_capability="propose"
    )
    return kb


# ===========================================================================
# Accept path
# ===========================================================================


class TestAcceptProposal:
    """Reviewer accepts a pending proposal → operations applied."""

    def test_accepted_proposal_state(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        assert proposal.state == "require_review"

        accepted = kb.accept_proposal(proposal.id, REVIEWER)
        assert accepted.state == "accepted"

    def test_accepted_proposal_has_decided_at(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        accepted = kb.accept_proposal(proposal.id, REVIEWER)
        assert accepted.decided_at is not None

    def test_assertion_written_after_accept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        # No assertion yet
        assert kb.assertions(subject=entity.id, predicate="Person.name") == []

        kb.accept_proposal(proposal.id, REVIEWER)

        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].value == "Ada"

    def test_accepted_assertion_carries_proposal_id(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        kb.accept_proposal(proposal.id, REVIEWER)

        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active[0].proposal_id == proposal.id

    def test_accepted_assertion_author_is_original_proposer(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        kb.accept_proposal(proposal.id, REVIEWER)

        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active[0].author == AI_AUTHOR

    def test_accept_retraction_proposal(self, tmp_path: Path) -> None:
        """Reviewer can accept an AI-proposed retraction."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        # Human creates an assertion (auto-accepted)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assertion_id = active[0].id

        # AI proposes retraction → require_review
        retract_proposal, _ = kb.retract(assertion_id, AI_AUTHOR)
        assert retract_proposal.state == "require_review"
        assert kb.assertions(subject=entity.id, predicate="Person.name", status="active") != []

        # Reviewer accepts
        kb.accept_proposal(retract_proposal.id, REVIEWER)
        assert kb.assertions(subject=entity.id, predicate="Person.name", status="active") == []
        retracted = kb.assertions(subject=entity.id, predicate="Person.name", status="retracted")
        assert len(retracted) == 1

    def test_accepted_assertion_goes_through_conflict_routing(self, tmp_path: Path) -> None:
        """Assertion accepted via review is still subject to SPEC §10 conflict routing."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        # Human writes "Ada" (auto-accepted, active)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)

        # AI proposes "Ava" → require_review
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ava", "Text", AI_AUTHOR, model="test-model-v1"
        )

        # Reviewer accepts → conflict routing triggers, both flagged
        kb.accept_proposal(proposal.id, REVIEWER)

        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert active == []
        assert len(flagged) == 2
        assert {a.value for a in flagged} == {"Ada", "Ava"}


# ===========================================================================
# Reject path
# ===========================================================================


class TestRejectProposal:
    """Reviewer rejects a pending proposal → no operations applied."""

    def test_rejected_proposal_state(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        rejected = kb.reject_proposal(proposal.id, REVIEWER)
        assert rejected.state == "rejected"

    def test_rejected_proposal_has_decided_at(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        rejected = kb.reject_proposal(proposal.id, REVIEWER)
        assert rejected.decided_at is not None

    def test_no_assertion_written_after_reject(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        kb.reject_proposal(proposal.id, REVIEWER, reason="Insufficient evidence")
        assert kb.assertions(subject=entity.id, predicate="Person.name") == []

    def test_reject_reason_stored(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        rejected = kb.reject_proposal(proposal.id, REVIEWER, reason="Insufficient evidence")
        assert rejected.policy_reason == "Insufficient evidence"


# ===========================================================================
# Guard rails
# ===========================================================================


class TestReviewGuards:
    """Capability checks and state guards."""

    def test_write_only_principal_cannot_accept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(CapabilityError, match="lacks review capability"):
            kb.accept_proposal(proposal.id, HUMAN_AUTHOR)

    def test_write_only_principal_cannot_reject(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(CapabilityError, match="lacks review capability"):
            kb.reject_proposal(proposal.id, HUMAN_AUTHOR)

    def test_unknown_reviewer_raises_on_accept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(AuthError, match="Principal not found"):
            kb.accept_proposal(proposal.id, "nobody@example.com")

    def test_unknown_reviewer_raises_on_reject(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )

        with pytest.raises(AuthError, match="Principal not found"):
            kb.reject_proposal(proposal.id, "nobody@example.com")

    def test_unknown_proposal_raises_on_accept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        with pytest.raises(NotFoundError, match="Proposal not found"):
            kb.accept_proposal("nonexistent-id", REVIEWER)

    def test_unknown_proposal_raises_on_reject(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        with pytest.raises(NotFoundError, match="Proposal not found"):
            kb.reject_proposal("nonexistent-id", REVIEWER)

    def test_already_accepted_proposal_raises(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        kb.accept_proposal(proposal.id, REVIEWER)

        with pytest.raises(ValidationError, match="not pending review"):
            kb.accept_proposal(proposal.id, REVIEWER)

    def test_already_rejected_proposal_raises(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI_AUTHOR, model="test-model-v1"
        )
        kb.reject_proposal(proposal.id, REVIEWER)

        with pytest.raises(ValidationError, match="not pending review"):
            kb.reject_proposal(proposal.id, REVIEWER)

    def test_auto_accepted_proposal_cannot_be_reviewed(self, tmp_path: Path) -> None:
        """auto_accepted proposals are already decided — review is not applicable."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN_AUTHOR)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_AUTHOR)
        assert proposal.state == "auto_accepted"

        with pytest.raises(ValidationError, match="not pending review"):
            kb.accept_proposal(proposal.id, REVIEWER)
